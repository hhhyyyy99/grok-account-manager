from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .models import Account, AccountStatus, LoginResult
from .reference import ReferenceProject
from .store import AccountStore
from .worker_runtime import BatchWorkerProcess, LogCallback


ProgressCallback = Callable[[LoginResult, int, int], Optional[LoginResult]]


@dataclass(frozen=True)
class LoginSettings:
    workers: int = 2
    timeout_seconds: int = 300
    proxy: str = ""
    headless: bool = False
    base_url: str = "https://cli-chat-proxy.grok.com/v1"
    probe_after_login: bool = False


class BatchLoginService:
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
            job_glob="login-*/input.json",
            busy_error="已有批量登录任务正在运行",
        )

    @property
    def running(self) -> bool:
        return self._worker.running

    def cancel(self) -> bool:
        return self._worker.cancel()

    def login_accounts(
        self,
        account_ids: Iterable[int],
        settings: LoginSettings,
        log: Optional[LogCallback] = None,
        progress: Optional[ProgressCallback] = None,
    ) -> List[LoginResult]:
        log = log or (lambda _: None)
        accounts = self.store.get_many(list(account_ids))
        if not accounts:
            return []
        ready = [account for account in accounts if account.has_login_credentials]
        missing = [account for account in accounts if not account.has_login_credentials]
        results: List[LoginResult] = []
        completed = 0
        total = len(accounts)
        for account in missing:
            result = LoginResult(account.id, account.email, False, "缺少邮箱或密码，无法登录")
            results.append(result)
            self.store.set_status([account.id], AccountStatus.INVALID.value, result.detail)
            completed += 1
            if progress:
                progress(result, completed, total)
        if not ready:
            return results

        self.project.validate()
        document = {
            "settings": {
                "workers": max(1, min(int(settings.workers), 10)),
                "timeout_seconds": max(60, int(settings.timeout_seconds)),
                "proxy": settings.proxy,
                "headless": bool(settings.headless),
                "base_url": settings.base_url,
                "probe": bool(settings.probe_after_login),
                "reuse_browser": True,
                "recycle_every": 10,
                "default_auth_dir": str(self.project.managed_auth_dir),
            },
            "accounts": [self._worker_account(account) for account in ready],
        }
        worker_results, parsed_ids, completed = self._worker.run(
            "batch-login",
            document,
            log=log,
            parse_result=self._handle_result,
            progress=progress,
            completed=completed,
            total=total,
            stdin_missing_message="批量登录 worker 未创建输入管道",
            start_failed_message="批量登录进程无法启动: %s",
            exit_failed_message="批量登录工作进程异常退出: %s",
        )
        results.extend(worker_results)

        for account in ready:
            if account.id in parsed_ids:
                continue
            result = LoginResult(account.id, account.email, False, "登录进程未返回该账号结果")
            results.append(result)
            self.store.set_status([account.id], AccountStatus.ERROR.value, result.detail)
            completed += 1
            if progress:
                progress(result, completed, total)
        results.sort(key=lambda item: item.account_id)
        return results

    def _worker_account(self, account: Account) -> Dict[str, Any]:
        return {
            "id": account.id,
            "email": account.email,
            "password": account.password,
            "auth_dir": str(self.project.managed_auth_dir),
        }

    def _remove_transient_auth_file(self, auth_file: str) -> None:
        if not auth_file:
            return
        try:
            target = Path(auth_file).expanduser().resolve()
            target.relative_to(self.project.managed_auth_dir.resolve())
            target.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

    def _handle_result(self, payload: str) -> LoginResult:
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            return LoginResult(0, "", False, "登录结果 JSON 无效: %s" % exc)
        account_id = int(value.get("id") or 0)
        email = str(value.get("email") or "")
        ok = bool(value.get("ok"))
        auth_file = str(value.get("path") or "")
        sso_token = str(value.get("sso_token") or "").strip()
        detail = str(value.get("error") or ("批量登录成功" if ok else "批量登录失败"))
        previous_sso_token = ""
        if ok:
            try:
                account = self.store.get(account_id)
                if account is None:
                    raise ValueError("登录结果对应的账号不存在")
                if account.email.casefold() != email.strip().casefold():
                    raise ValueError("登录结果邮箱与账号不匹配")
                previous_sso_token = account.sso_token
                auth = json.loads(Path(auth_file).read_text(encoding="utf-8-sig"))
                auth_email = str(auth.get("email") or "").strip()
                if auth_email and account.email.casefold() != auth_email.casefold():
                    raise ValueError("凭据文件邮箱与账号不匹配")
                access_token = str(auth.get("access_token") or "")
                refresh_token = str(auth.get("refresh_token") or "")
                if not access_token or not refresh_token:
                    raise ValueError("凭据文件缺少 access_token/refresh_token")
                # Persist managed credentials immediately on login success. External
                # pool sync (CPA hotload / Grok2API) is best-effort and must not
                # delay or reverse this write.
                if not sso_token:
                    raise ValueError("浏览器未返回新的 SSO cookie")
                self.store.apply_login_credentials(
                    account_id,
                    access_token,
                    refresh_token,
                    str(auth.get("expired") or ""),
                    "",
                    detail="SSO 与 CPA 凭据已刷新",
                    sso_token=sso_token,
                )
            except (OSError, json.JSONDecodeError, ValueError, AttributeError) as exc:
                ok = False
                detail = "登录成功但凭据回写失败: %s" % exc
                self._remove_transient_auth_file(auth_file)
                auth_file = ""
                sso_token = ""
        elif auth_file:
            self._remove_transient_auth_file(auth_file)
            auth_file = ""
            sso_token = ""
        if not ok and account_id:
            self.store.set_status([account_id], AccountStatus.ERROR.value, detail)
            sso_token = ""
        return LoginResult(
            account_id,
            email,
            ok,
            detail,
            auth_file,
            previous_sso_token=previous_sso_token,
            sso_token=sso_token if ok else "",
        )
