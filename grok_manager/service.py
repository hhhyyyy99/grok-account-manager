from __future__ import annotations

import shutil
import sys
from dataclasses import replace
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import ConfigStore, ManagerConfig
from .inspection import InspectionService, TokenInspector, expiration_for
from .login import BatchLoginService, LoginSettings
from .password_reset import BatchPasswordResetService, PasswordResetSettings
from .models import (
    Account,
    AccountStatus,
    CpaRefreshResult,
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
        output_root = self.reference.output_dir.resolve()
        for artifact in files:
            resolved = self._resolved_data_root_path(artifact, data_root)
            if resolved is not None:
                batch_dir = self._batch_dir_for_artifact(resolved, output_root)
                if batch_dir is not None:
                    batch_dirs.add(batch_dir)
            self._delete_data_root_artifact(artifact, data_root)
        for auth_path in used_auth_files:
            resolved = self._resolved_data_root_path(auth_path, data_root)
            if resolved is not None:
                batch_dir = self._batch_dir_for_artifact(resolved, output_root)
                if batch_dir is not None:
                    batch_dirs.add(batch_dir)
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
    def _batch_dir_for_artifact(path: Path, output_root: Path) -> Optional[Path]:
        """Return registration-output batch dir only; never climb to data root."""
        try:
            resolved = path.expanduser().resolve()
            relative = resolved.relative_to(output_root)
        except (OSError, ValueError):
            return None
        if not relative.parts:
            return None
        # registration-output/<batch>/...
        return (output_root / relative.parts[0]).resolve()

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

    @staticmethod
    def _pending_sso_secret_name(email: str) -> str:
        return "pending-sso-replace:%s" % str(email or "").strip().lower()

    @staticmethod
    def _parse_pending_sso_secret(raw: str) -> tuple[str, str]:
        text = str(raw or "").strip()
        if not text:
            return "", ""
        if "\n" in text:
            old, _, new = text.partition("\n")
            return old.strip(), new.strip()
        # Legacy format stored only the old remote token.
        return text, ""

    def _remember_pending_sso_replace(
        self,
        email: str,
        previous_token: str,
        new_token: str = "",
        *,
        advance_old: bool = False,
    ) -> None:
        previous = str(previous_token or "").strip()
        fresh = str(new_token or "").strip()
        normalized_email = str(email or "").strip().lower()
        if not normalized_email or self.vault is None or not self.vault.is_unlocked:
            return
        if not previous and not fresh:
            return
        secret = self._pending_sso_secret_name(normalized_email)
        try:
            existing_raw = self.vault.get_secret(secret)
        except Exception:
            existing_raw = ""
        existing_old, existing_new = self._parse_pending_sso_secret(existing_raw)

        if advance_old and previous:
            # Explicit chain progress after the remote reports the previous old
            # token is already gone.
            old = previous
        else:
            # Preserve the original remote token that still needs to be replaced.
            old = existing_old or previous
        # Advance the "already attempted new token" when a later login produces a
        # newer value. Keep the first intermediate token if the caller only
        # re-sends the same pending pair.
        if fresh and fresh not in {old, existing_old}:
            new = fresh
        else:
            new = existing_new or fresh
        if old and new and old == new:
            new = ""
        if not old and new:
            # No remote previous known; nothing to recover later.
            return
        payload = old if not new else "%s\n%s" % (old, new)
        if payload == existing_raw:
            return
        self.vault.put_secret(secret, payload)

    def _resolve_pending_sso_replace(
        self, email: str, fallback: str = ""
    ) -> tuple[str, str]:
        normalized_email = str(email or "").strip().lower()
        if normalized_email and self.vault is not None and self.vault.is_unlocked:
            try:
                stored = self.vault.get_secret(self._pending_sso_secret_name(normalized_email))
            except Exception:
                stored = ""
            old, new = self._parse_pending_sso_secret(stored)
            if old or new:
                return old, new
        return str(fallback or "").strip(), ""

    def _clear_pending_sso_replace(self, email: str) -> None:
        normalized_email = str(email or "").strip().lower()
        if not normalized_email or self.vault is None or not self.vault.is_unlocked:
            return
        try:
            self.vault.delete_secret(self._pending_sso_secret_name(normalized_email))
        except Exception:
            pass

    def _previous_tokens_for_remote(
        self, email: str, previous_token: str = "", current_token: str = ""
    ) -> List[str]:
        pending_old, pending_new = self._resolve_pending_sso_replace(email, previous_token)
        current = str(current_token or "").strip()
        candidates: List[str] = []
        for token in (
            pending_old,
            previous_token,
            pending_new,
        ):
            value = str(token or "").strip()
            if not value:
                continue
            # Keep same-token candidates so the remote layer can verify presence
            # when previous == current.
            if value not in candidates:
                candidates.append(value)
        if not candidates and current:
            # Explicit same-token relogin with no pending state still needs the
            # remote existence check path.
            candidates.append(current)
        return candidates

    def _sync_relogin_credentials(self, result: LoginResult, log=None) -> str:
        """Best-effort external sync after credentials are already persisted."""
        notes: List[str] = []
        try:
            previous_for_remote, pending_new = self._resolve_pending_sso_replace(
                result.email, result.previous_sso_token
            )
            sso_token = str(result.sso_token or "").strip()
            if not sso_token:
                account = self.store.get(result.account_id) if result.account_id else None
                sso_token = str(account.sso_token if account else "").strip()
            seed_previous = result.previous_sso_token or previous_for_remote
            if seed_previous and seed_previous != sso_token:
                self._remember_pending_sso_replace(
                    result.email,
                    seed_previous,
                    sso_token or pending_new,
                )
                previous_for_remote, pending_new = self._resolve_pending_sso_replace(
                    result.email, previous_for_remote
                )

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

            if not sso_token:
                message = "Grok2API 未同步: 没有可用的 SSO token"
                notes.append(message)
                if log:
                    log("[%s] %s" % (result.email, message))
                return "；".join(notes)

            grok_log = None
            if log:
                grok_log = lambda message: log("[%s] %s" % (result.email, message))

            previous_candidates = self._previous_tokens_for_remote(
                result.email,
                previous_token=result.previous_sso_token or previous_for_remote,
                current_token=sso_token,
            )
            if not previous_candidates:
                previous_candidates = [sso_token or ""]

            sync_errors: List[str] = []
            for index, previous_token in enumerate(previous_candidates):
                try:
                    self.reference.sync_grok2api(
                        sso_token,
                        email=result.email,
                        log_callback=grok_log,
                        previous_token=previous_token,
                    )
                except Exception as exc:
                    sync_errors.append(str(exc))
                    # If the original old token is gone because a previous attempt
                    # already replaced it to pending_new, advance and retry with
                    # that intermediate token as the next previous candidate.
                    detail = str(exc)
                    is_missing = "未找到待替换凭据" in detail or "account_not_found" in detail
                    has_next = index + 1 < len(previous_candidates)
                    if is_missing and has_next:
                        advanced_old = previous_candidates[index + 1]
                        self._remember_pending_sso_replace(
                            result.email,
                            advanced_old,
                            sso_token,
                            advance_old=True,
                        )
                        if log:
                            log(
                                "[%s] 远端旧 token 已不存在，改用中间 token 继续替换"
                                % result.email
                            )
                        continue
                    message = "Grok2API 未同步: %s" % exc
                    notes.append(message)
                    if log:
                        log("[%s] %s" % (result.email, message))
                    break
                else:
                    self._clear_pending_sso_replace(result.email)
                    sync_errors = []
                    break
            if sync_errors and not any("Grok2API 未同步" in item for item in notes):
                message = "Grok2API 未同步: %s" % sync_errors[-1]
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
        # Only the canonical browser signal may trigger auto password reset.
        detail = str(result.detail or "").strip()
        return detail == "邮箱或密码错误" or detail.endswith("邮箱或密码错误")

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

    @staticmethod
    def _refresh_failure_allows_sso_remint(detail: str) -> bool:
        text = str(detail or "").lower()
        markers = (
            "invalid_grant",
            "revoked",
            "expired",
            "invalid_token",
            "缺少 refresh_token",
        )
        return any(marker in text for marker in markers)

    def _silent_refresh_cpa_account(
        self,
        account: Account,
        *,
        proxy: str,
        base_url: str,
        timeout: float,
        log,
    ) -> CpaRefreshResult:
        from grok_register.cpa_xai.oauth_device import OAuthDeviceError, refresh_access_token
        from grok_register.cpa_xai.schema import build_cpa_xai_auth
        from grok_register.cpa_xai.writer import write_cpa_xai_auth

        refresh_token = str(account.refresh_token or "").strip()
        if not refresh_token:
            return CpaRefreshResult(
                account.id,
                account.email,
                False,
                "缺少 refresh_token",
            )
        try:
            token = refresh_access_token(
                refresh_token,
                timeout=timeout,
                proxy=proxy or None,
            )
            payload = build_cpa_xai_auth(
                email=account.email,
                access_token=token.access_token,
                refresh_token=token.refresh_token,
                id_token=token.id_token,
                expires_in=token.expires_in,
                base_url=base_url,
            )
            auth_path = write_cpa_xai_auth(
                self.reference.managed_auth_dir,
                payload,
            )
            self.store.apply_cpa_credentials(
                account.id,
                token.access_token,
                token.refresh_token,
                str(payload.get("expired") or ""),
                str(auth_path),
                detail="CPA 凭据已续期",
            )
            detail = "CPA 凭据已续期"
            try:
                hotload_path = self.reference.sync_cpa_hotload(auth_path)
            except Exception as exc:
                note = "CPA hotload 未同步: %s" % exc
                detail = "%s；%s" % (detail, note)
                log("[%s] %s" % (account.email, note))
            else:
                if hotload_path is not None:
                    log("[%s] CPA hotload 已更新: %s" % (account.email, hotload_path))
            log("[%s] CPA silent refresh 成功" % account.email)
            return CpaRefreshResult(
                account.id,
                account.email,
                True,
                detail,
                auth_file=str(auth_path),
            )
        except OAuthDeviceError as exc:
            return CpaRefreshResult(
                account.id,
                account.email,
                False,
                "CPA 续期失败: %s" % exc,
            )
        except Exception as exc:
            return CpaRefreshResult(
                account.id,
                account.email,
                False,
                "CPA 续期异常: %s" % exc,
            )

    def _sync_cpa_hotload_for_result(
        self,
        result: CpaRefreshResult,
        log,
    ) -> CpaRefreshResult:
        if not result.ok or not result.auth_file:
            return result
        try:
            hotload_path = self.reference.sync_cpa_hotload(result.auth_file)
        except Exception as exc:
            note = "CPA hotload 未同步: %s" % exc
            log("[%s] %s" % (result.email, note))
            return replace(result, detail="%s；%s" % (result.detail, note))
        if hotload_path is not None:
            log("[%s] CPA hotload 已更新: %s" % (result.email, hotload_path))
        return result

    def batch_refresh_cpa(
        self,
        account_ids: Iterable[int],
        log=None,
        progress=None,
        cancelled=None,
    ) -> List[CpaRefreshResult]:
        """Renew CPA tokens: silent refresh first, then SSO device remint."""
        from grok_register.cpa_xai.schema import DEFAULT_BASE_URL

        log = log or (lambda _message: None)
        ids = [int(account_id) for account_id in account_ids]
        accounts = self.store.get_many(ids)
        by_id = {account.id: account for account in accounts}
        registration_config = self.reference.load_registration_config()
        proxy = str(
            registration_config.get("cpa_proxy") or registration_config.get("proxy") or ""
        ).strip()
        base_url = str(
            registration_config.get("cpa_base_url") or DEFAULT_BASE_URL
        ).strip() or DEFAULT_BASE_URL
        timeout = float(self.config.probe_timeout_seconds or 30)
        results_by_id: Dict[int, CpaRefreshResult] = {}
        remint_ids: List[int] = []
        total = len(ids)
        completed = 0

        for account_id in ids:
            if cancelled and cancelled():
                break
            account = by_id.get(account_id)
            if account is None:
                result = CpaRefreshResult(account_id, "", False, "账号不存在")
                results_by_id[account_id] = result
                completed += 1
                if progress:
                    progress(result, completed, total)
                continue

            silent = self._silent_refresh_cpa_account(
                account,
                proxy=proxy,
                base_url=base_url,
                timeout=timeout,
                log=log,
            )
            if silent.ok:
                results_by_id[account.id] = silent
                completed += 1
                if progress:
                    progress(silent, completed, total)
                continue

            sso_token = str(account.sso_token or "").strip()
            if sso_token and self._refresh_failure_allows_sso_remint(silent.detail):
                log(
                    "[%s] silent refresh 不可用（%s），改用 SSO 重新签发 CPA"
                    % (account.email, silent.detail)
                )
                remint_ids.append(account.id)
                continue

            if not sso_token:
                detail = "%s；缺少可用 SSO，请改用批量登录" % silent.detail
            else:
                detail = "%s；请改用批量登录" % silent.detail
            result = CpaRefreshResult(account.id, account.email, False, detail)
            self.store.set_status(
                [account.id],
                AccountStatus.NEEDS_LOGIN.value
                if account.has_login_credentials
                else AccountStatus.EXPIRED.value,
                detail,
            )
            results_by_id[account.id] = result
            completed += 1
            log("[%s] %s" % (account.email, detail))
            if progress:
                progress(result, completed, total)

        if remint_ids and not (cancelled and cancelled()):
            settings = LoginSettings(
                workers=self.config.login_workers,
                timeout_seconds=self.config.login_timeout_seconds,
                proxy=proxy,
                headless=bool(registration_config.get("cpa_headless", False)),
                base_url=base_url,
                probe_after_login=False,
            )
            log("开始通过 SSO 重新签发 %s 个账号的 CPA 凭据" % len(remint_ids))

            def remint_progress(result: CpaRefreshResult, _done: int, _total: int):
                nonlocal completed
                final = self._sync_cpa_hotload_for_result(result, log)
                results_by_id[result.account_id] = final
                completed += 1
                if progress:
                    progress(final, completed, total)
                return final

            remint_results = self.login.remint_cpa_via_sso(
                remint_ids,
                settings,
                log=log,
                progress=remint_progress,
            )
            for result in remint_results:
                if result.account_id in results_by_id:
                    continue
                final = self._sync_cpa_hotload_for_result(result, log)
                results_by_id[result.account_id] = final

        results = [
            results_by_id[account_id]
            for account_id in ids
            if account_id in results_by_id
        ]
        refreshed_ids = [
            result.account_id for result in results if result.ok and result.account_id
        ]
        if refreshed_ids:
            log("CPA 续期完成，开始复核 %s 个账号" % len(refreshed_ids))
            reviews = self.inspect_accounts(
                refreshed_ids,
                live=self.config.live_probe,
            )
            reviews_by_id = {review.account_id: review for review in reviews}
            reviewed: List[CpaRefreshResult] = []
            for result in results:
                review = reviews_by_id.get(result.account_id)
                if review is None:
                    reviewed.append(result)
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
                reviewed.append(
                    replace(result, detail="%s；%s" % (result.detail, summary))
                )
            results = reviewed
        return results

    def diagnostics(self) -> List[Tuple[bool, str]]:
        return self.reference.diagnostics(self.python_executable)

    def active_cpa_account_ids(self) -> List[int]:
        return self.store.ids_for_cpa_statuses([AccountStatus.ACTIVE.value])

    def cpa_accounts_needing_refresh(
        self,
        *,
        lead_seconds: Optional[int] = None,
        account_ids: Optional[Iterable[int]] = None,
    ) -> List[Account]:
        """CPA-active accounts whose access token is missing or within the lead window."""
        lead = (
            int(lead_seconds)
            if lead_seconds is not None
            else int(self.config.cpa_guard_lead_seconds)
        )
        lead = max(0, lead)
        now = datetime.now(timezone.utc)
        deadline = now + timedelta(seconds=lead)
        if account_ids is None:
            ids = self.active_cpa_account_ids()
        else:
            ids = [int(value) for value in account_ids]
        accounts = self.store.get_many(ids)
        selected: List[Account] = []
        for account in accounts:
            if account.cpa_status != AccountStatus.ACTIVE.value:
                continue
            if not str(account.access_token or "").strip() and not str(
                account.refresh_token or ""
            ).strip():
                continue
            expires_at = expiration_for(account)
            if expires_at is None or expires_at <= deadline:
                selected.append(account)
        return selected

    def guard_cpa_tokens(
        self,
        *,
        lead_seconds: Optional[int] = None,
        account_ids: Optional[Iterable[int]] = None,
        log=None,
        progress=None,
        cancelled=None,
        reinspect: bool = True,
    ) -> List[CpaRefreshResult]:
        """Keep CPA-active accounts fresh via silent refresh only (no browser remint).

        - Only accounts with cpa_status=active are considered.
        - access_token within lead window (or already past / unparseable) → refresh_token.
        - refresh failure / missing refresh → mark CPA expired.
        """
        from grok_register.cpa_xai.schema import DEFAULT_BASE_URL

        log = log or (lambda _message: None)
        candidates = self.cpa_accounts_needing_refresh(
            lead_seconds=lead_seconds,
            account_ids=account_ids,
        )
        if not candidates:
            log("CPA 守护：没有需要续期的 active CPA 账号")
            return []

        registration_config = self.reference.load_registration_config()
        proxy = str(
            registration_config.get("cpa_proxy") or registration_config.get("proxy") or ""
        ).strip()
        base_url = str(
            registration_config.get("cpa_base_url") or DEFAULT_BASE_URL
        ).strip() or DEFAULT_BASE_URL
        timeout = float(self.config.probe_timeout_seconds or 30)
        results: List[CpaRefreshResult] = []
        total = len(candidates)
        log(
            "CPA 守护：%s 个 active CPA 账号进入 silent refresh（提前 %ss）"
            % (
                total,
                int(lead_seconds if lead_seconds is not None else self.config.cpa_guard_lead_seconds),
            )
        )

        for index, account in enumerate(candidates, start=1):
            if cancelled and cancelled():
                break
            expires_at = expiration_for(account)
            log(
                "[%s] access 到期 %s，开始 silent refresh"
                % (account.email, expires_at.isoformat() if expires_at else "未知")
            )
            silent = self._silent_refresh_cpa_account(
                account,
                proxy=proxy,
                base_url=base_url,
                timeout=timeout,
                log=log,
            )
            if silent.ok:
                results.append(silent)
                if progress:
                    progress(silent, index, total)
                continue

            detail = "CPA 凭据已过期: %s" % silent.detail
            self.store.mark_cpa_expired(account.id, detail)
            result = CpaRefreshResult(account.id, account.email, False, detail)
            results.append(result)
            log("[%s] %s" % (account.email, detail))
            if progress:
                progress(result, index, total)

        refreshed_ids = [
            result.account_id for result in results if result.ok and result.account_id
        ]
        if reinspect and refreshed_ids:
            log("CPA 守护：续期成功 %s 个，开始复核" % len(refreshed_ids))
            reviews = self.inspect_accounts(
                refreshed_ids,
                live=self.config.live_probe,
            )
            reviews_by_id = {review.account_id: review for review in reviews}
            reviewed: List[CpaRefreshResult] = []
            for result in results:
                review = reviews_by_id.get(result.account_id)
                if review is None:
                    reviewed.append(result)
                    continue
                summary = "复核 SSO=%s，CPA=%s" % (
                    status_label(review.sso_status),
                    status_label(review.cpa_status),
                )
                reviewed.append(
                    replace(result, detail="%s；%s" % (result.detail, summary))
                )
            results = reviewed
        return results

    def run_cpa_guard_loop(
        self,
        *,
        interval_seconds: Optional[int] = None,
        lead_seconds: Optional[int] = None,
        once: bool = False,
        log=None,
        cancelled=None,
    ) -> None:
        """Daemon loop: periodically silent-refresh soon-to-expire active CPA tokens."""
        import time

        log = log or (lambda _message: None)
        interval = (
            int(interval_seconds)
            if interval_seconds is not None
            else int(self.config.cpa_guard_interval_seconds)
        )
        interval = max(30, interval)
        lead = (
            int(lead_seconds)
            if lead_seconds is not None
            else int(self.config.cpa_guard_lead_seconds)
        )
        log(
            "CPA 守护进程启动：interval=%ss lead=%ss once=%s"
            % (interval, lead, once)
        )
        while True:
            if cancelled and cancelled():
                log("CPA 守护进程已停止")
                return
            try:
                results = self.guard_cpa_tokens(
                    lead_seconds=lead,
                    log=log,
                    cancelled=cancelled,
                )
                ok = sum(1 for item in results if item.ok)
                failed = len(results) - ok
                log("CPA 守护本轮完成：处理 %s，成功 %s，标记过期 %s" % (len(results), ok, failed))
            except Exception as exc:
                log("CPA 守护本轮异常: %s" % exc)
            if once:
                return
            # Interruptible sleep.
            deadline = time.time() + interval
            while time.time() < deadline:
                if cancelled and cancelled():
                    log("CPA 守护进程已停止")
                    return
                time.sleep(min(1.0, max(0.0, deadline - time.time())))
