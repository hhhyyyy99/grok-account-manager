from __future__ import annotations

import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from .config import ConfigStore, ManagerConfig
from .inspection import InspectionService, TokenInspector
from .login import BatchLoginService, LoginSettings
from .password_reset import BatchPasswordResetService, PasswordResetSettings
from .models import (
    Account,
    AccountStatus,
    InspectionResult,
    LoginResult,
    PasswordResetResult,
    status_label,
)
from .reference import (
    ReferenceProject,
    RegistrationRequest,
    RegistrationResult,
    RegistrationRunner,
)
from .store import AccountStore
from .paths import migrate_legacy_install_data
from .vault import CredentialVault, VaultLockedError


class GrokManager:
    """Application facade used by both the local Web UI and CLI."""

    def __init__(
        self,
        config_store: Optional[ConfigStore] = None,
        store: Optional[AccountStore] = None,
        reference: Optional[ReferenceProject] = None,
        python_executable: str = "",
        vault: Optional[CredentialVault] = None,
    ):
        migrate_legacy_install_data()
        self.config_store = config_store or ConfigStore()
        self.config = self.config_store.load()
        self.reference = reference or ReferenceProject()
        migration_vault = store.vault if store is not None else vault
        if migration_vault is not None:
            self.reference.credential_vault = migration_vault
        self.reference.migrate_legacy_data()
        if store is not None:
            self.store = store
            self.vault = store.vault
        elif vault is not None:
            self.vault = vault
            self.store = AccountStore(vault=vault)
        else:
            raise VaultLockedError("启动管理端前必须先解锁凭据保险库")
        self.python_executable = python_executable or sys.executable
        self.reference.ensure_registration_config()
        self._wire_adapters()

    def _wire_adapters(self) -> None:
        self.registration = RegistrationRunner(self.reference, self.python_executable)
        self.inspection = InspectionService(
            self.store,
            TokenInspector(timeout_seconds=self.config.probe_timeout_seconds),
            max_workers=self.config.inspection_workers,
        )
        self.login = BatchLoginService(self.store, self.reference, self.python_executable)
        self.password_reset = BatchPasswordResetService(self.store, self.reference, self.python_executable)

    def save_manager_config(self, config: ManagerConfig) -> None:
        self.config_store.save(config)
        self.config = config
        self._wire_adapters()

    def import_reference_accounts(
        self,
        account_files: Optional[Sequence[Path]] = None,
        extra_auth_dirs: Sequence[Path] = (),
    ) -> List[Account]:
        files = (
            [Path(path) for path in account_files]
            if account_files is not None
            else self.reference.discover_account_files()
        )
        drafts = self.reference.import_records(files, extra_auth_dirs)
        imported_emails = set()
        used_auth_files: set[str] = set()
        batch_dirs: set[Path] = set()
        for draft in drafts:
            imported_emails.add(str(draft.email or "").strip().lower())
            if draft.auth_file:
                try:
                    used_auth_files.add(str(Path(draft.auth_file).expanduser().resolve()))
                except OSError:
                    pass
            self.reference.find_mail_credential(draft.email, draft.source)
        accounts = self.store.upsert_many(
            replace(draft, auth_file="") for draft in drafts
        )
        data_root = self.reference.data_root.resolve()
        for artifact in files:
            resolved = self._resolved_data_root_path(artifact, data_root)
            if resolved is not None:
                batch_dirs.add(resolved.parent)
            self._delete_data_root_artifact(artifact, data_root)
        for auth_path in used_auth_files:
            resolved = self._resolved_data_root_path(auth_path, data_root)
            if resolved is not None:
                parent = resolved.parent
                batch_dirs.add(parent.parent if parent.name == "cpa_auths" else parent)
            self._delete_data_root_artifact(auth_path, data_root)
        self._prune_imported_mail_credentials(imported_emails, data_root)
        for batch_dir in batch_dirs:
            self._delete_data_root_tree(batch_dir / "sub2api_exports", data_root)
        return accounts

    @staticmethod
    def _resolved_data_root_path(path: Path | str, data_root: Path) -> Optional[Path]:
        try:
            resolved = Path(path).expanduser().resolve()
            resolved.relative_to(data_root)
            return resolved
        except (OSError, ValueError):
            return None

    @staticmethod
    def _delete_data_root_artifact(path: Path | str, data_root: Path) -> None:
        resolved = GrokManager._resolved_data_root_path(path, data_root)
        if resolved is None:
            return
        try:
            resolved.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _delete_data_root_tree(path: Path | str, data_root: Path) -> None:
        resolved = GrokManager._resolved_data_root_path(path, data_root)
        if resolved is None or not resolved.exists():
            return
        try:
            if resolved.is_dir():
                shutil.rmtree(resolved)
            else:
                resolved.unlink(missing_ok=True)
        except OSError:
            pass

    def _prune_imported_mail_credentials(
        self, imported_emails: set[str], data_root: Path
    ) -> None:
        if not imported_emails:
            return
        for path in self.reference.discover_mail_credential_files():
            try:
                resolved = Path(path).expanduser().resolve()
                resolved.relative_to(data_root)
            except (OSError, ValueError):
                continue
            try:
                lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            remaining: List[str] = []
            changed = False
            for line in lines:
                address, separator, _credential = line.partition("\t")
                if separator and address.strip().casefold() in imported_emails:
                    changed = True
                    continue
                remaining.append(line)
            if not changed:
                continue
            if remaining:
                try:
                    resolved.write_text(
                        "\n".join(remaining) + ("\n" if remaining else ""),
                        encoding="utf-8",
                    )
                except OSError:
                    pass
            else:
                try:
                    resolved.unlink(missing_ok=True)
                except OSError:
                    pass

    def import_account_text(self, text: str, source: str = "manual-import") -> List[Account]:
        auth_index = self.reference.build_auth_index()
        drafts = self.reference.parse_account_text(text, source=source, auth_index=auth_index)
        return self.store.upsert_many(drafts)

    def run_registration(
        self,
        request: RegistrationRequest,
        log: Optional[Callable[[str], None]] = None,
    ) -> RegistrationResult:
        result = self.registration.run(request, log=log)
        imported = []
        if Path(result.accounts_file).is_file():
            extra_dirs = [Path(result.batch_dir)] if result.batch_dir else []
            imported = self.import_reference_accounts([Path(result.accounts_file)], extra_dirs)
        if result.ok and not imported:
            return replace(
                result,
                ok=False,
                imported_count=0,
                error="注册进程已结束，但没有产生可导入账号",
            )
        return replace(result, imported_count=len(imported))

    def inspect_accounts(
        self,
        account_ids: Iterable[int],
        live: Optional[bool] = None,
        progress=None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> List[InspectionResult]:
        use_live = self.config.live_probe if live is None else bool(live)
        try:
            registration_config = self.reference.load_registration_config()
            self.inspection.inspector.proxy = str(
                registration_config.get("cpa_proxy")
                or registration_config.get("proxy")
                or ""
            ).strip()
            configured_hotload = str(
                registration_config.get("cpa_hotload_dir") or ""
            ).strip()
            if configured_hotload:
                hotload_path = Path(configured_hotload).expanduser()
                if not hotload_path.is_absolute():
                    hotload_path = self.reference.data_root / hotload_path
                self.inspection.inspector.cpa_hotload_dir = hotload_path.resolve()
            else:
                self.inspection.inspector.cpa_hotload_dir = None
        except Exception:
            self.inspection.inspector.proxy = ""
            self.inspection.inspector.cpa_hotload_dir = None
        return self.inspection.inspect_accounts(
            account_ids,
            live=use_live,
            progress=progress,
            cancelled=cancelled,
        )

    def _sync_relogin_credentials(self, result: LoginResult, log=None) -> str:
        """Best-effort external sync after credentials are already persisted."""
        notes: List[str] = []
        try:
            try:
                hotload_path = self.reference.sync_cpa_hotload(result.auth_file)
            except Exception as exc:
                message = "CPA hotload 未同步: %s" % exc
                notes.append(message)
                if log:
                    log("[%s] %s" % (result.email, message))
            else:
                if hotload_path is not None and log:
                    log("[%s] CPA hotload 已更新: %s" % (result.email, hotload_path))

            sso_token = str(result.sso_token or "").strip()
            if not sso_token:
                # Prefer the SSO already written by the login handler.
                account = self.store.get(result.account_id) if result.account_id else None
                sso_token = str(account.sso_token if account else "").strip()
            if not sso_token:
                message = "Grok2API 未同步: 没有可用的 SSO token"
                notes.append(message)
                if log:
                    log("[%s] %s" % (result.email, message))
                return "；".join(notes)

            grok_log = None
            if log:
                grok_log = lambda message: log("[%s] %s" % (result.email, message))
            try:
                self.reference.sync_grok2api(
                    sso_token,
                    email=result.email,
                    log_callback=grok_log,
                    previous_token=result.previous_sso_token,
                )
            except Exception as exc:
                message = "Grok2API 未同步: %s" % exc
                notes.append(message)
                if log:
                    log("[%s] %s" % (result.email, message))
            return "；".join(notes)
        finally:
            self.login._remove_transient_auth_file(result.auth_file)

    @staticmethod
    def _is_wrong_password_login(result: LoginResult) -> bool:
        if result.ok:
            return False
        detail = str(result.detail or "")
        low = detail.casefold()
        markers = (
            "邮箱或密码错误",
            "邮箱地址或密码错误",
            "电子邮箱或密码不正确",
            "邮箱或密码不正确",
            "密码不正确",
            "密码错误",
            "wrong email address or password",
            "incorrect email or password",
            "invalid email or password",
            "email or password is incorrect",
            "the password you entered is incorrect",
            "incorrect password",
            "invalid credentials",
        )
        if any(marker in low or marker in detail for marker in markers):
            return True
        return (
            ("password" in low or "密码" in detail)
            and any(word in low for word in ("incorrect", "invalid", "wrong", "错误", "不正确"))
        )

    def _auto_reset_login_failures(self, results: List[LoginResult], log=None, progress=None) -> List[LoginResult]:
        candidates = [
            result
            for result in results
            if result.account_id and self._is_wrong_password_login(result)
        ]
        if not candidates:
            return results
        log = log or (lambda _message: None)
        candidate_ids = [result.account_id for result in candidates]
        log("检测到 %s 个账号邮箱或密码错误，开始自动重置密码" % len(candidates))
        for result in candidates:
            log("[%s] 登录密码错误，开始自动重置密码" % result.email)
        try:
            reset_results = self.reset_passwords(candidate_ids, log=log)
        except Exception as exc:
            log("自动重置密码任务失败: %s" % exc)
            return [
                replace(
                    result,
                    detail="%s；自动重置密码任务失败：%s" % (result.detail, exc),
                )
                if result in candidates
                else result
                for result in results
            ]
        reset_by_id = {result.account_id: result for result in reset_results}
        retry_ids = [
            result.account_id
            for result in candidates
            if reset_by_id.get(result.account_id) and reset_by_id[result.account_id].ok
        ]
        retries: List[LoginResult] = []
        if retry_ids:
            log("自动重置密码完成 %s 个，开始使用新密码重新登录" % len(retry_ids))
            retries = self.batch_login(
                retry_ids,
                log=log,
                progress=None,
                auto_reset_password=False,
                _skip_review=True,
            )
        retry_by_id = {result.account_id: result for result in retries}
        merged: List[LoginResult] = []
        candidate_id_set = set(candidate_ids)
        for result in results:
            if result.account_id not in candidate_id_set:
                merged.append(result)
                continue
            reset_result = reset_by_id.get(result.account_id)
            if reset_result is None:
                merged.append(
                    replace(result, detail="%s；自动重置密码未返回结果" % result.detail)
                )
                continue
            if not reset_result.ok:
                merged.append(
                    replace(
                        result,
                        detail="%s；自动重置密码失败：%s"
                        % (result.detail, reset_result.detail),
                    )
                )
                continue
            retry = retry_by_id.get(result.account_id)
            if retry is None:
                merged.append(
                    replace(result, detail="密码已重置，但重新登录未返回结果")
                )
                continue
            merged.append(
                replace(
                    retry,
                    detail="自动重置密码后：%s" % retry.detail,
                )
            )
            if progress:
                progress(retry, len(results), len(results))
        return merged

    def batch_login(
        self,
        account_ids: Iterable[int],
        log=None,
        progress=None,
        auto_reset_password: bool = True,
        _skip_review: bool = False,
    ) -> List[LoginResult]:
        log = log or (lambda _message: None)
        registration_config = self.reference.load_registration_config()
        settings = LoginSettings(
            workers=self.config.login_workers,
            timeout_seconds=self.config.login_timeout_seconds,
            proxy=str(registration_config.get("cpa_proxy") or registration_config.get("proxy") or ""),
            headless=bool(registration_config.get("cpa_headless", False)),
            base_url=str(
                registration_config.get("cpa_base_url")
                or "https://cli-chat-proxy.grok.com/v1"
            ),
            probe_after_login=False,
        )

        def handle_result(result: LoginResult, completed: int, total: int):
            final = result
            if result.ok and result.account_id:
                try:
                    sync_note = self._sync_relogin_credentials(result, log=log)
                    detail = result.detail or "批量登录成功"
                    if sync_note:
                        detail = "%s；%s" % (detail, sync_note)
                        if log:
                            log("[%s] 登录成功，但部分同步未完成: %s" % (result.email, sync_note))
                    final = replace(result, auth_file="", detail=detail)
                except Exception as exc:
                    # Unexpected failures still keep login success; only annotate.
                    detail = "%s；凭据同步异常: %s" % (
                        result.detail or "批量登录成功",
                        exc,
                    )
                    final = replace(result, auth_file="", detail=detail)
                    if log:
                        log("[%s] %s" % (result.email, detail))
            elif result.auth_file:
                self.login._remove_transient_auth_file(result.auth_file)
                final = replace(result, auth_file="")
            if progress:
                progress(final, completed, total)
            return final

        results = self.login.login_accounts(
            account_ids,
            settings,
            log=log,
            progress=handle_result,
        )
        if auto_reset_password:
            results = self._auto_reset_login_failures(results, log=log, progress=progress)
        if _skip_review:
            return results
        refreshed = [result for result in results if result.ok and result.account_id]
        refreshed_ids = [result.account_id for result in refreshed]
        if refreshed_ids:
            log("登录完成，开始复核新的 SSO 与 CPA token")
            reviews = self.inspect_accounts(
                refreshed_ids,
                live=self.config.live_probe,
            )
            reviews_by_id = {review.account_id: review for review in reviews}
            reviewed_results: List[LoginResult] = []
            for result in results:
                review = reviews_by_id.get(result.account_id)
                if review is None:
                    reviewed_results.append(result)
                    continue
                summary = "复核 SSO=%s，CPA=%s" % (
                    status_label(review.sso_status),
                    status_label(review.cpa_status),
                )
                log(
                    "[%s] 复核完成: SSO=%s，CPA=%s"
                    % (
                        result.email,
                        status_label(review.sso_status),
                        status_label(review.cpa_status),
                    )
                )
                log("[%s] 复核详情: %s" % (result.email, review.detail))
                reviewed_results.append(
                    replace(result, detail="%s；%s" % (result.detail, summary))
                )
            results = reviewed_results
        return results

    def reset_passwords(
        self,
        account_ids: Iterable[int],
        log=None,
        progress=None,
    ) -> List[PasswordResetResult]:
        registration_config = self.reference.load_registration_config()
        settings = PasswordResetSettings(
            workers=self.config.login_workers,
            timeout_seconds=self.config.login_timeout_seconds,
            proxy=str(registration_config.get("cpa_proxy") or registration_config.get("proxy") or "").strip(),
            headless=bool(registration_config.get("cpa_headless", False)),
        )
        return self.password_reset.reset_accounts(
            account_ids,
            settings,
            log=log,
            progress=progress,
        )

    def relogin_candidate_ids(self) -> List[int]:
        return self.store.ids_for_statuses(
            [
                AccountStatus.EXPIRED.value,
                AccountStatus.INVALID.value,
                AccountStatus.NEEDS_LOGIN.value,
            ]
        )

    def diagnostics(self) -> List[Tuple[bool, str]]:
        return self.reference.diagnostics(self.python_executable)
