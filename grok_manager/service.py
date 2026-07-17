from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from .config import ConfigStore, ManagerConfig
from .inspection import InspectionService, TokenInspector
from .login import BatchLoginService, LoginSettings
from .models import Account, AccountStatus, InspectionResult, LoginResult
from .reference import (
    ReferenceProject,
    RegistrationRequest,
    RegistrationResult,
    RegistrationRunner,
)
from .store import AccountStore


class GrokManager:
    """Application facade used by both the local Web UI and CLI."""

    def __init__(self, config_store: Optional[ConfigStore] = None, store: Optional[AccountStore] = None):
        self.config_store = config_store or ConfigStore()
        self.config = self.config_store.load()
        self.store = store or AccountStore()
        self._wire_adapters()

    def _wire_adapters(self) -> None:
        self.reference = ReferenceProject(self.config.reference_path)
        python_executable = self.config.resolve_reference_python()
        self.registration = RegistrationRunner(self.reference, python_executable)
        self.inspection = InspectionService(
            self.store,
            TokenInspector(timeout_seconds=self.config.probe_timeout_seconds),
            max_workers=self.config.inspection_workers,
        )
        self.login = BatchLoginService(self.store, self.reference, python_executable)

    def save_manager_config(self, config: ManagerConfig) -> None:
        self.config_store.save(config)
        self.config = config
        self._wire_adapters()

    def import_reference_accounts(
        self,
        account_files: Optional[Sequence[Path]] = None,
        extra_auth_dirs: Sequence[Path] = (),
    ) -> List[Account]:
        drafts = self.reference.import_records(account_files, extra_auth_dirs)
        return self.store.upsert_many(drafts)

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
    ) -> List[InspectionResult]:
        use_live = self.config.live_probe if live is None else bool(live)
        try:
            registration_config = self.reference.load_registration_config()
            self.inspection.inspector.proxy = str(
                registration_config.get("cpa_proxy")
                or registration_config.get("proxy")
                or ""
            ).strip()
        except Exception:
            self.inspection.inspector.proxy = ""
        return self.inspection.inspect_accounts(account_ids, live=use_live, progress=progress)

    def batch_login(self, account_ids: Iterable[int], log=None, progress=None) -> List[LoginResult]:
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
        results = self.login.login_accounts(account_ids, settings, log=log, progress=progress)
        refreshed_ids = [result.account_id for result in results if result.ok and result.account_id]
        if refreshed_ids:
            if log:
                log("登录完成，开始复核新的 SSO 与 CPA token")
            self.inspect_accounts(refreshed_ids, live=self.config.live_probe)
        return results

    def relogin_candidate_ids(self) -> List[int]:
        return self.store.ids_for_statuses(
            [
                AccountStatus.EXPIRED.value,
                AccountStatus.INVALID.value,
                AccountStatus.NEEDS_LOGIN.value,
            ]
        )

    def diagnostics(self) -> List[Tuple[bool, str]]:
        return self.reference.diagnostics(self.config.resolve_reference_python())
