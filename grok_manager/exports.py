from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from grok_register.cpa_to_sub2api import (
    build_sub2api_document,
    cpa_xai_to_sub2api_account,
)

from .models import Account


@dataclass(frozen=True)
class AccountExport:
    filename: str
    content_type: str
    body: bytes
    exported_count: int
    skipped_count: int


class AccountExporter:
    SUPPORTED_FORMATS = frozenset(("cpa", "sub2api", "grok2api", "accounts"))

    def __init__(self, registration_config: Optional[Dict[str, Any]] = None):
        self.registration_config = registration_config or {}

    def export(self, accounts: Iterable[Account], export_format: str) -> AccountExport:
        selected = list(accounts)
        normalized = str(export_format or "").strip().lower()
        if normalized not in self.SUPPORTED_FORMATS:
            raise ValueError("导出格式必须是 cpa、sub2api、grok2api 或 accounts")
        if not selected:
            raise ValueError("没有符合条件的账号可导出")
        if normalized == "cpa":
            return self._export_cpa(selected)
        if normalized == "sub2api":
            return self._export_sub2api(selected)
        if normalized == "accounts":
            return self._export_accounts(selected)
        return self._export_grok2api(selected)

    def _export_cpa(self, accounts: List[Account]) -> AccountExport:
        output = io.BytesIO()
        exported = 0
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for account in accounts:
                payload = self._cpa_payload(account)
                if payload is None:
                    continue
                archive.writestr(
                    "xai-%s.json" % self._safe_email(account),
                    self._json_bytes(payload),
                )
                exported += 1
        self._require_exported(exported, "CPA")
        return AccountExport(
            filename="grok-cpa-%s.zip" % self._timestamp(),
            content_type="application/zip",
            body=output.getvalue(),
            exported_count=exported,
            skipped_count=len(accounts) - exported,
        )

    def _export_sub2api(self, accounts: List[Account]) -> AccountExport:
        converted = []
        for account in accounts:
            payload = self._cpa_payload(account)
            if payload is None:
                continue
            converted.append(
                cpa_xai_to_sub2api_account(payload, source="grok_account_manager")
            )
        self._require_exported(len(converted), "Sub2API")
        document = build_sub2api_document(converted)
        return AccountExport(
            filename="grok-sub2api-%s.json" % self._timestamp(),
            content_type="application/json; charset=utf-8",
            body=self._json_bytes(document),
            exported_count=len(converted),
            skipped_count=len(accounts) - len(converted),
        )

    def _export_grok2api(self, accounts: List[Account]) -> AccountExport:
        entries = []
        seen = set()
        for account in accounts:
            token = self._normalize_sso(account.sso_token)
            if not token or token in seen:
                continue
            seen.add(token)
            entries.append(
                {
                    "token": token,
                    "tags": ["grok-account-manager"],
                    "note": account.email,
                }
            )
        self._require_exported(len(entries), "Grok2API")
        pool_name = str(
            self.registration_config.get("grok2api_pool_name") or "ssoBasic"
        ).strip() or "ssoBasic"
        return AccountExport(
            filename="grok2api-tokens-%s.json" % self._timestamp(),
            content_type="application/json; charset=utf-8",
            body=self._json_bytes({pool_name: entries}),
            exported_count=len(entries),
            skipped_count=len(accounts) - len(entries),
        )

    def _export_accounts(self, accounts: List[Account]) -> AccountExport:
        rows: List[str] = []
        for account in accounts:
            email = str(account.email or "").strip().lower()
            password = str(account.password or "").strip()
            token = self._normalize_sso(account.sso_token)
            if not email or not password or not token:
                continue
            rows.append("%s\t%s\t%s" % (email, password, token))
        self._require_exported(len(rows), "账户")
        lines = ["账户\t密码\ttoken", *rows]
        body = ("\n".join(lines) + "\n").encode("utf-8")
        return AccountExport(
            filename="accounts_%s.txt" % self._timestamp(),
            content_type="text/plain; charset=utf-8",
            body=body,
            exported_count=len(rows),
            skipped_count=len(accounts) - len(rows),
        )

    def _cpa_payload(self, account: Account) -> Optional[Dict[str, Any]]:
        payload: Dict[str, Any] = {}
        if account.auth_file:
            try:
                loaded = json.loads(
                    Path(account.auth_file).read_text(encoding="utf-8-sig")
                )
            except (OSError, json.JSONDecodeError):
                loaded = {}
            if isinstance(loaded, dict):
                loaded_email = str(loaded.get("email") or "").strip().lower()
                if not loaded_email or loaded_email == account.email.strip().lower():
                    payload.update(loaded)

        payload["email"] = account.email
        if account.access_token:
            payload["access_token"] = account.access_token
        if account.refresh_token:
            payload["refresh_token"] = account.refresh_token
        if account.token_expires_at:
            payload["expired"] = account.token_expires_at
        payload.setdefault(
            "base_url",
            str(
                self.registration_config.get("cpa_base_url")
                or "https://cli-chat-proxy.grok.com/v1"
            ),
        )
        payload.setdefault("token_type", "Bearer")
        payload.setdefault("token_endpoint", "https://auth.x.ai/oauth2/token")
        payload.setdefault("redirect_uri", "http://127.0.0.1:56121/callback")
        payload.setdefault("headers", {})
        if not str(payload.get("access_token") or "").strip():
            return None
        return payload

    @staticmethod
    def _normalize_sso(raw_token: str) -> str:
        token = str(raw_token or "").strip()
        return token[4:] if token.startswith("sso=") else token

    @staticmethod
    def _safe_email(account: Account) -> str:
        value = re.sub(r"[^A-Za-z0-9@._+-]+", "_", account.email).strip("._")
        return value or "account-%s" % account.id

    @staticmethod
    def _json_bytes(value: Any) -> bytes:
        return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    @staticmethod
    def _require_exported(count: int, label: str) -> None:
        if count <= 0:
            raise ValueError("所选账号没有可导出的 %s 凭据" % label)
