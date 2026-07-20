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
    auto_reset_password: bool = False
    require_account_gates: bool = True


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
        self._remint_expected_snapshot: Dict[int, Dict[str, Optional[str]]] = {}
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
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> List[LoginResult]:
        log = log or (lambda _: None)
        is_cancelled = cancelled or (lambda: False)
        requested_ids: List[int] = []
        seen_ids: set[int] = set()
        for value in account_ids:
            account_id = int(value or 0)
            if account_id > 0 and account_id not in seen_ids:
                seen_ids.add(account_id)
                requested_ids.append(account_id)
        fetched = self.store.get_many(requested_ids)
        by_id = {account.id: account for account in fetched}
        accounts = [by_id[account_id] for account_id in requested_ids if account_id in by_id]
        if not accounts:
            return []
        ready = [account for account in accounts if account.has_login_credentials]
        missing = [account for account in accounts if not account.has_login_credentials]
        results: List[LoginResult] = []
        completed = 0
        total = len(accounts)

        def ordered_results() -> List[LoginResult]:
            results_by_id = {result.account_id: result for result in results}
            return [
                results_by_id[account_id]
                for account_id in requested_ids
                if account_id in results_by_id
            ]

        def append_cancelled(pending: List[Account]) -> None:
            nonlocal completed
            handled_ids = {result.account_id for result in results}
            for account in pending:
                if account.id in handled_ids:
                    continue
                result = LoginResult(account.id, account.email, False, "任务已取消")
                results.append(result)
                handled_ids.add(account.id)
                completed += 1
                if progress:
                    progress(result, completed, total)

        for account in missing:
            if is_cancelled():
                append_cancelled(accounts)
                return ordered_results()
            result = LoginResult(account.id, account.email, False, "缺少邮箱或密码，无法登录")
            results.append(result)
            self.store.set_status([account.id], AccountStatus.INVALID.value, result.detail)
            completed += 1
            if progress:
                progress(result, completed, total)
        if not ready:
            return results

        self.project.validate()
        auto_reset = bool(settings.auto_reset_password)
        # Prefetch only local/vault mail JWTs. Network admin recovery is deferred to
        # the wrong-password path inside the browser worker so large batches cannot
        # freeze the management UI before the first login starts.
        log("正在准备 %s 个账号的登录任务" % len(ready))
        worker_accounts: List[Dict[str, Any]] = []
        for index, account in enumerate(ready, 1):
            if is_cancelled():
                # Nothing has been handed to the browser worker yet, so every
                # prepared/unprepared ready account must be marked cancelled.
                append_cancelled(ready)
                return ordered_results()
            worker_accounts.append(
                self._worker_account(account, include_mail_credential=auto_reset)
            )
            if index == 1 or index == len(ready) or index % 50 == 0:
                log("已准备登录输入 %s/%s" % (index, len(ready)))
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
                "auto_reset_password": auto_reset,
                "require_account_gates": bool(settings.require_account_gates),
            },
            "accounts": worker_accounts,
        }
        if is_cancelled():
            append_cancelled(ready)
            return ordered_results()
        log(
            "启动浏览器登录 worker：%s 个账号，并发 %s"
            % (len(worker_accounts), document["settings"]["workers"])
        )
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
            detail = "任务已取消" if is_cancelled() else "登录进程未返回该账号结果"
            result = LoginResult(account.id, account.email, False, detail)
            results.append(result)
            if detail != "任务已取消":
                self.store.set_status([account.id], AccountStatus.ERROR.value, result.detail)
            completed += 1
            if progress:
                progress(result, completed, total)
        return ordered_results()

    def remint_cpa_via_sso(
        self,
        account_ids: Iterable[int],
        settings: LoginSettings,
        log: Optional[LogCallback] = None,
        progress: Optional[CpaRemintProgressCallback] = None,
    ) -> List[CpaRefreshResult]:
        """Remint CPA tokens by injecting a live SSO cookie into device OAuth."""
        log = log or (lambda _: None)
        requested_ids: List[int] = []
        seen_ids: set[int] = set()
        for value in account_ids:
            account_id = int(value or 0)
            if account_id > 0 and account_id not in seen_ids:
                seen_ids.add(account_id)
                requested_ids.append(account_id)
        fetched = self.store.get_many(requested_ids)
        by_id = {account.id: account for account in fetched}
        accounts = [by_id[account_id] for account_id in requested_ids if account_id in by_id]
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
                # Remint is CPA-focused; skip TOS gate unless caller opts in.
                "require_account_gates": bool(settings.require_account_gates),
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
        results_by_id = {result.account_id: result for result in results}
        return [results_by_id[account_id] for account_id in requested_ids if account_id in results_by_id]

    def _worker_account(
        self,
        account: Account,
        *,
        include_mail_credential: bool = False,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "id": account.id,
            "email": account.email,
            "password": account.password,
            "auth_dir": str(self.project.managed_auth_dir),
        }
        if include_mail_credential:
            # Local/vault only. Network admin recovery happens later inside the
            # browser worker when a wrong-password reset is actually needed.
            credential = self.project.find_mail_credential(
                account.email,
                account.source,
                allow_admin_recover=False,
            )
            payload["mail_credential"] = str(credential or "")
        return payload

    def _persist_recovered_password(
        self,
        account_id: int,
        email: str,
        password: str,
    ) -> None:
        password = str(password or "").strip()
        if not account_id or not password:
            return
        try:
            account = self.store.get(account_id)
            if account is None:
                return
            if email and account.email.casefold() != email.strip().casefold():
                return
            self.store.apply_password_reset(account_id, password)
            try:
                self.project.persist_account_password(
                    account.email, password, account.source
                )
            except Exception:
                # DB already has the password; artifact write is best-effort.
                pass
        except Exception:
            pass

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
        recovered = bool(value.get("recovered_from_wrong_password"))
        new_password = str(value.get("password") or "").strip()
        detail = str(
            value.get("detail")
            or value.get("error")
            or ("批量登录成功" if ok else "批量登录失败")
        )
        previous_sso_token = ""
        if new_password and account_id:
            self._persist_recovered_password(account_id, email, new_password)
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
                if recovered and not detail.startswith("自动重置密码后"):
                    detail = "自动重置密码后：%s" % (detail or "批量登录成功")
            except (OSError, json.JSONDecodeError, ValueError, AttributeError) as exc:
                ok = False
                detail = "登录成功但凭据回写失败: %s" % exc
                if recovered:
                    detail = "自动重置密码后：%s" % detail
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
        expected_refresh = expected.get("refresh") if expected else None
        expected_access = expected.get("access") if expected else None
        expected_cpa_updated_at = expected.get("cpa_updated_at") if expected else None
        if ok:
            try:
                account = self.store.get(account_id)
                if account is None:
                    raise ValueError("SSO 续期结果对应的账号不存在")
                if account.email.casefold() != email.strip().casefold():
                    raise ValueError("SSO 续期结果邮箱与账号不匹配")
                if expected is not None:
                    current_snapshot = {
                        "refresh": str(account.refresh_token or "").strip(),
                        "access": str(account.access_token or "").strip(),
                        "cpa_updated_at": str(getattr(account, "cpa_updated_at", "") or "").strip(),
                    }
                    if current_snapshot != expected:
                        self._remove_transient_auth_file(auth_file)
                        return CpaRefreshResult(
                            account_id,
                            email,
                            True,
                            "凭据已由并发任务更新，丢弃本次旧 SSO 续期结果",
                        )
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
