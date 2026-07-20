from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional

from .models import Account, AccountStatus, PasswordResetResult
from .reference import ReferenceProject
from .store import AccountStore
from .worker_runtime import BatchWorkerProcess, LogCallback


ProgressCallback = Callable[[PasswordResetResult, int, int], Optional[PasswordResetResult]]


@dataclass(frozen=True)
class PasswordResetSettings:
    workers: int = 2
    timeout_seconds: int = 300
    proxy: str = ""
    headless: bool = False


class BatchPasswordResetService:
    def __init__(
        self,
        store: AccountStore,
        project: ReferenceProject,
        python_executable: str,
    ):
        self.store = store
        self.project = project
        self.python_executable = python_executable
        self._worker = BatchWorkerProcess(
            project,
            python_executable,
            job_glob="password-reset-*/input.json",
            busy_error="已有密码重置任务正在运行",
        )

    @property
    def running(self) -> bool:
        return self._worker.running

    def cancel(self) -> bool:
        return self._worker.cancel()

    def reset_accounts(
        self,
        account_ids: Iterable[int],
        settings: PasswordResetSettings,
        log: Optional[LogCallback] = None,
        progress: Optional[ProgressCallback] = None,
    ) -> List[PasswordResetResult]:
        log = log or (lambda _message: None)
        accounts = self.store.get_many(list(account_ids))
        if not accounts:
            return []
        ready: List[tuple[Account, str]] = []
        results: List[PasswordResetResult] = []
        completed = 0
        total = len(accounts)
        for account in accounts:
            credential = self.project.find_mail_credential(account.email, account.source)
            if not credential:
                # Last-chance admin recovery for missing local JWT (show_password path).
                credential = self.project.recover_mail_credential_via_admin(account.email)
            if not credential:
                result = PasswordResetResult(
                    account.id,
                    account.email,
                    False,
                    "找不到该账号的邮箱访问凭据，且管理员接口未能恢复 JWT，无法接收重置验证码",
                )
                results.append(result)
                self.store.set_status([account.id], AccountStatus.ERROR.value, result.detail)
                completed += 1
                if progress:
                    progress(result, completed, total)
            else:
                ready.append((account, credential))
                log("[%s] 已准备邮箱访问凭据，开始密码重置" % account.email)
        if not ready:
            return results

        self.project.validate()
        document = {
            "settings": {
                "workers": max(1, min(int(settings.workers), 10)),
                "timeout_seconds": max(60, int(settings.timeout_seconds)),
                "proxy": settings.proxy,
                "headless": bool(settings.headless),
            },
            "accounts": [
                {
                    "id": account.id,
                    "email": account.email,
                    "mail_credential": credential,
                }
                for account, credential in ready
            ],
        }
        worker_results, parsed_ids, completed = self._worker.run(
            "reset-password",
            document,
            log=log,
            parse_result=self._handle_result,
            progress=progress,
            completed=completed,
            total=total,
            stdin_missing_message="密码重置 worker 未创建输入管道",
            start_failed_message="密码重置进程无法启动: %s",
            exit_failed_message="密码重置工作进程异常退出: %s",
        )
        results.extend(worker_results)

        for account, _credential in ready:
            if account.id in parsed_ids:
                continue
            result = PasswordResetResult(
                account.id, account.email, False, "密码重置进程未返回该账号结果"
            )
            results.append(result)
            self.store.set_status([account.id], AccountStatus.ERROR.value, result.detail)
            completed += 1
            if progress:
                progress(result, completed, total)
        results.sort(key=lambda item: item.account_id)
        return results

    def _handle_result(self, payload: str) -> PasswordResetResult:
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            return PasswordResetResult(0, "", False, "密码重置结果 JSON 无效: %s" % exc)
        account_id = int(value.get("id") or 0)
        email = str(value.get("email") or "")
        ok = bool(value.get("ok"))
        detail = str(value.get("error") or ("密码已重置" if ok else "密码重置失败"))
        if ok:
            try:
                account = self.store.get(account_id)
                if account is None:
                    raise ValueError("密码重置结果对应的账号不存在")
                if account.email.casefold() != email.strip().casefold():
                    raise ValueError("密码重置结果邮箱与账号不匹配")
                password = str(value.get("password") or "").strip()
                if not password:
                    raise ValueError("重置结果缺少新密码")
                self.store.apply_password_reset(account_id, password)
                try:
                    path = self.project.persist_account_password(
                        account.email, password, account.source
                    )
                    detail = "密码已重置，已写入 %s" % path.name
                except Exception as exc:
                    detail = "密码已重置，数据库已更新但产物写入失败: %s" % exc
            except (ValueError, OSError) as exc:
                ok = False
                detail = "密码已重置但回写失败: %s" % exc
        if not ok and account_id:
            self.store.set_status([account_id], AccountStatus.ERROR.value, detail)
        return PasswordResetResult(account_id, email, ok, detail)
