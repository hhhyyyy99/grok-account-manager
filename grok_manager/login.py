from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .models import Account, AccountStatus, CpaRefreshResult, LoginResult
from .reference import ReferenceProject
from .store import AccountStore
from .worker_runtime import BatchWorkerProcess, LogCallback


ProgressCallback = Callable[[LoginResult, int, int], Optional[LoginResult]]
CpaRemintProgressCallback = Callable[[CpaRefreshResult, int, int], Optional[CpaRefreshResult]]


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
        self._remint_expected_snapshot: Dict[int, Dict[str, str]] = {}
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

    def remint_cpa_via_sso(
        self,
        account_ids: Iterable[int],
        settings: LoginSettings,
        log: Optional[LogCallback] = None,
        progress: Optional[CpaRemintProgressCallback] = None,
    ) -> List[CpaRefreshResult]:
        """Remint CPA tokens by injecting a live SSO cookie into device OAuth."""
        log = log or (lambda _: None)
        accounts = self.store.get_many(list(account_ids))
        if not accounts:
            return []
        ready = [account for account in accounts if str(account.sso_token or "").strip()]
        missing = [account for account in accounts if not str(account.sso_token or "").strip()]
        results: List[CpaRefreshResult] = []
        completed = 0
        total = len(accounts)
        for account in missing:
            result = CpaRefreshResult(
                account.id,
                account.email,
                False,
                "缺少可用 SSO，请改用批量登录",
            )
            results.append(result)
            self.store.set_status(
                [account.id],
                AccountStatus.NEEDS_LOGIN.value
                if account.has_login_credentials
                else AccountStatus.INVALID.value,
                result.detail,
            )
            completed += 1
            if progress:
                progress(result, completed, total)
        if not ready:
            return results

        # Capture CPA versions before the long-running remint worker so failure
        # marking cannot treat a concurrent rotation as the pre-remint baseline.
        expected_by_id = {
            account.id: {
                "refresh": str(account.refresh_token or "").strip(),
                "access": str(account.access_token or "").strip(),
                "cpa_updated_at": str(getattr(account, "cpa_updated_at", "") or "").strip(),
            }
            for account in ready
        }
        self._remint_expected_snapshot = expected_by_id

        self.project.validate()
        document = {
            "settings": {
                "workers": max(1, min(int(settings.workers), 10)),
                "timeout_seconds": max(60, int(settings.timeout_seconds)),
                "proxy": settings.proxy,
                "headless": bool(settings.headless),
                "base_url": settings.base_url,
                "probe": False,
                "reuse_browser": True,
                "recycle_every": 10,
                "default_auth_dir": str(self.project.managed_auth_dir),
            },
            "accounts": [self._worker_remint_account(account) for account in ready],
        }
        try:
            worker_results, parsed_ids, completed = self._worker.run(
                "batch-login",
                document,
                log=log,
                parse_result=self._handle_remint_result,
                progress=progress,
                completed=completed,
                total=total,
                stdin_missing_message="CPA SSO 续期 worker 未创建输入管道",
                start_failed_message="CPA SSO 续期进程无法启动: %s",
                exit_failed_message="CPA SSO 续期工作进程异常退出: %s",
            )
        finally:
            self._remint_expected_snapshot = {}
        results.extend(worker_results)
        for account in ready:
            if account.id in parsed_ids:
                continue
            result = CpaRefreshResult(
                account.id,
                account.email,
                False,
                "SSO 续期进程未返回该账号结果",
            )
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

    def _worker_remint_account(self, account: Account) -> Dict[str, Any]:
        return {
            "id": account.id,
            "email": account.email,
            "password": "",
            "sso_token": account.sso_token,
            "allow_passwordless": True,
            "auth_dir": str(self.project.managed_auth_dir),
        }

    def _remove_transient_auth_file(self, auth_file: str) -> None:
        from .paths import remove_managed_auth_file

        remove_managed_auth_file(auth_file, self.project.managed_auth_dir)

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

    def _handle_remint_result(self, payload: str) -> CpaRefreshResult:
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            return CpaRefreshResult(0, "", False, "SSO 续期结果 JSON 无效: %s" % exc)
        account_id = int(value.get("id") or 0)
        email = str(value.get("email") or "")
        ok = bool(value.get("ok"))
        auth_file = str(value.get("path") or "")
        detail = str(
            value.get("error")
            or ("通过 SSO 重新签发 CPA 凭据" if ok else "SSO 续期失败")
        )
        expected = (getattr(self, "_remint_expected_snapshot", {}) or {}).get(account_id) or {}
        expected_refresh = str(expected.get("refresh") or "")
        expected_access = str(expected.get("access") or "")
        expected_cpa_updated_at = str(expected.get("cpa_updated_at") or "")
        if ok:
            try:
                account = self.store.get(account_id)
                if account is None:
                    raise ValueError("SSO 续期结果对应的账号不存在")
                if account.email.casefold() != email.strip().casefold():
                    raise ValueError("SSO 续期结果邮箱与账号不匹配")
                auth = json.loads(Path(auth_file).read_text(encoding="utf-8-sig"))
                auth_email = str(auth.get("email") or "").strip()
                if auth_email and account.email.casefold() != auth_email.casefold():
                    raise ValueError("凭据文件邮箱与账号不匹配")
                access_token = str(auth.get("access_token") or "").strip()
                refresh_token = str(auth.get("refresh_token") or "").strip()
                if not access_token or not refresh_token:
                    raise ValueError("凭据文件缺少 access_token/refresh_token")
                self.store.apply_cpa_credentials(
                    account_id,
                    access_token,
                    refresh_token,
                    str(auth.get("expired") or ""),
                    "",
                    detail="通过 SSO 重新签发 CPA 凭据",
                )
                detail = "通过 SSO 重新签发 CPA 凭据"
            except (OSError, json.JSONDecodeError, ValueError, AttributeError) as exc:
                ok = False
                detail = "SSO 续期成功但凭据回写失败: %s" % exc
                self._remove_transient_auth_file(auth_file)
                auth_file = ""
        elif auth_file:
            self._remove_transient_auth_file(auth_file)
            auth_file = ""
        if not ok and account_id:
            # Keep cpa_status in sync so guardian stops retrying revoked accounts,
            # but never clobber a concurrent successful rotation.
            self.store.mark_cpa_expired_if_refresh_unchanged(
                account_id,
                expected_refresh,
                detail,
                expected_access=expected_access,
                expected_cpa_updated_at=expected_cpa_updated_at,
            )
        return CpaRefreshResult(
            account_id,
            email,
            ok,
            detail,
            # Caller syncs hotload then deletes this temporary managed auth file.
            auth_file=auth_file if ok else "",
        )
