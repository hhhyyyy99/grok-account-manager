from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class AccountStatus(str, Enum):
    UNKNOWN = "unknown"
    ACTIVE = "active"
    EXPIRED = "expired"
    INVALID = "invalid"
    NEEDS_LOGIN = "needs_login"
    LIMITED = "limited"
    ERROR = "error"
    CHECKING = "checking"
    LOGGING_IN = "logging_in"
    MISSING_CPA = "missing_cpa"


STATUS_LABELS = {
    AccountStatus.UNKNOWN.value: "未巡检",
    AccountStatus.ACTIVE.value: "正常",
    AccountStatus.EXPIRED.value: "已过期",
    AccountStatus.INVALID.value: "凭据无效",
    AccountStatus.NEEDS_LOGIN.value: "待登录",
    AccountStatus.LIMITED.value: "受限但有效",
    AccountStatus.ERROR.value: "巡检异常",
    AccountStatus.CHECKING.value: "巡检中",
    AccountStatus.LOGGING_IN.value: "登录中",
    AccountStatus.MISSING_CPA.value: "缺少 CPA 凭据",
}


def status_label(value: str) -> str:
    return STATUS_LABELS.get(value, value or STATUS_LABELS[AccountStatus.UNKNOWN.value])


@dataclass(frozen=True)
class AccountDraft:
    email: str
    password: str = ""
    sso_token: str = ""
    access_token: str = ""
    refresh_token: str = ""
    token_expires_at: str = ""
    auth_file: str = ""
    source: str = ""
    source_modified_at: str = ""


@dataclass(frozen=True)
class Account:
    id: int
    email: str
    password: str
    sso_token: str
    access_token: str
    refresh_token: str
    token_expires_at: str
    sso_expires_at: str
    auth_file: str
    source: str
    source_modified_at: str
    status: str
    status_detail: str
    sso_status: str
    sso_detail: str
    cpa_status: str
    cpa_detail: str
    last_checked_at: str
    last_login_at: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Account":
        return cls(**{field: row[field] for field in cls.__dataclass_fields__})

    @property
    def status_label(self) -> str:
        return status_label(self.status)

    @property
    def sso_status_label(self) -> str:
        return status_label(self.sso_status)

    @property
    def cpa_status_label(self) -> str:
        return status_label(self.cpa_status)

    @property
    def has_login_credentials(self) -> bool:
        return bool(self.email and self.password)

    @property
    def missing_cpa_credentials(self) -> bool:
        return not self.access_token and not self.auth_file


@dataclass(frozen=True)
class InspectionResult:
    account_id: int
    status: str
    detail: str
    checked_at: str
    expires_at: str = ""
    http_status: Optional[int] = None
    sso_status: str = ""
    sso_detail: str = ""
    cpa_status: str = ""
    cpa_detail: str = ""
    sso_expires_at: str = ""


@dataclass(frozen=True)
class LoginResult:
    account_id: int
    email: str
    ok: bool
    detail: str
    auth_file: str = ""
