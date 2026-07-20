from __future__ import annotations

import base64
import binascii
import json
import urllib.error
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from .models import Account, AccountStatus, InspectionResult, utc_now_iso
from .store import AccountStore


DEFAULT_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
SSO_ACCOUNT_URL = "https://accounts.x.ai/account"
DEFAULT_HEADERS = {
    "x-grok-client-version": "0.2.93",
    "x-xai-token-auth": "xai-grok-cli",
    "x-authenticateresponse": "authenticate-response",
    "x-grok-client-identifier": "grok-shell",
    "User-Agent": "grok-shell/0.2.93 (grok-account-manager)",
}
SSO_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
    ),
}


def parse_utc(value: str) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_sso_token(token: str) -> str:
    value = str(token or "").strip()
    return value[4:] if value.startswith("sso=") else value


def jwt_expiration(token: str) -> Optional[datetime]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return None
    try:
        payload_part = parts[1] + ("=" * (-len(parts[1]) % 4))
        payload = json.loads(base64.urlsafe_b64decode(payload_part.encode("ascii")))
        return datetime.fromtimestamp(int(payload["exp"]), tz=timezone.utc)
    except (
        ValueError,
        OverflowError,
        OSError,
        KeyError,
        TypeError,
        UnicodeError,
        json.JSONDecodeError,
        binascii.Error,
    ):
        return None


def expiration_for(account: Account) -> Optional[datetime]:
    return parse_utc(account.token_expires_at) or jwt_expiration(account.access_token)


def iso_or_empty(value: Optional[datetime]) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class CredentialCheck:
    status: str
    detail: str
    expires_at: str = ""
    http_status: Optional[int] = None


class TokenInspector:
    """Inspect both the registered SSO session and optional CPA OAuth token."""

    def __init__(
        self,
        timeout_seconds: int = 20,
        expiry_skew_seconds: int = 30,
        proxy: str = "",
        cpa_hotload_dir: str = "",
        cpa_base_url: str = "",
    ):
        self.timeout_seconds = max(3, int(timeout_seconds))
        self.expiry_skew = timedelta(seconds=max(0, int(expiry_skew_seconds)))
        self.proxy = str(proxy or "").strip()
        configured_hotload = str(cpa_hotload_dir or "").strip()
        self.cpa_hotload_dir = (
            Path(configured_hotload).expanduser().resolve()
            if configured_hotload
            else None
        )
        self.cpa_base_url = str(cpa_base_url or "").strip().rstrip("/")

    def _opener(self) -> urllib.request.OpenerDirector:
        if self.proxy:
            return urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})
            )
        return urllib.request.build_opener()

    def inspect(self, account: Account, live: bool = True) -> InspectionResult:
        checked_at = utc_now_iso()
        sso = self._inspect_sso(account, live)
        cpa = self._inspect_cpa(account, live)
        status, detail = self._combine(account, sso, cpa)
        return InspectionResult(
            account_id=account.id,
            status=status,
            detail=detail,
            checked_at=checked_at,
            expires_at=cpa.expires_at,
            http_status=cpa.http_status if cpa.http_status is not None else sso.http_status,
            sso_status=sso.status,
            sso_detail=sso.detail,
            cpa_status=cpa.status,
            cpa_detail=cpa.detail,
            sso_expires_at=sso.expires_at,
            observed_access_token=str(account.access_token or "").strip(),
            observed_cpa_updated_at=str(getattr(account, "cpa_updated_at", "") or "").strip(),
            cpa_snapshot=True,
            observed_sso_token=str(account.sso_token or "").strip(),
            observed_last_login_at=str(getattr(account, "last_login_at", "") or "").strip(),
            sso_snapshot=True,
        )

    def _inspect_sso(self, account: Account, live: bool) -> CredentialCheck:
        token = normalize_sso_token(account.sso_token)
        if not token:
            if account.has_login_credentials:
                return CredentialCheck(
                    AccountStatus.NEEDS_LOGIN.value,
                    "尚无 SSO cookie，可批量登录获取",
                )
            return CredentialCheck(AccountStatus.INVALID.value, "缺少 SSO cookie 和登录凭据")

        expires = jwt_expiration(token)
        expires_at = iso_or_empty(expires)
        if expires is not None and expires <= datetime.now(timezone.utc) + self.expiry_skew:
            return CredentialCheck(
                AccountStatus.EXPIRED.value,
                "SSO cookie 已于 %s 过期" % expires_at,
                expires_at,
            )
        if not live:
            if expires is None:
                return CredentialCheck(
                    AccountStatus.UNKNOWN.value,
                    "SSO cookie 为不透明 token，需要在线巡检才能确认",
                )
            return CredentialCheck(
                AccountStatus.ACTIVE.value,
                "SSO 本地有效期检查通过，到期时间 %s" % expires_at,
                expires_at,
            )
        return self._probe_sso(token, expires_at)

    def _probe_sso(self, token: str, expires_at: str) -> CredentialCheck:
        headers = dict(SSO_HEADERS)
        headers["Cookie"] = "sso=%s; sso-rw=%s" % (token, token)
        request = urllib.request.Request(SSO_ACCOUNT_URL, headers=headers, method="GET")
        try:
            with self._opener().open(request, timeout=self.timeout_seconds) as response:
                status_code = int(getattr(response, "status", 200) or 200)
                final_url = str(response.geturl() or "").lower()
                body = response.read(256 * 1024).decode("utf-8", errors="replace").lower()
            login_markers = (
                "/sign-in",
                "/signin",
                "/login",
                "continue with email",
                "sign in with email",
                "使用邮箱登录",
            )
            if "/account" in final_url and "sign-in" not in final_url:
                return CredentialCheck(
                    AccountStatus.ACTIVE.value,
                    "SSO 在线巡检通过",
                    expires_at,
                    status_code,
                )
            if any(marker in final_url for marker in login_markers) or any(
                marker in body for marker in login_markers[3:]
            ):
                return CredentialCheck(
                    AccountStatus.EXPIRED.value,
                    "SSO 在线巡检已跳转到登录页",
                    expires_at,
                    status_code,
                )
            return CredentialCheck(
                AccountStatus.UNKNOWN.value,
                "SSO 请求成功，但最终页面无法确认登录状态",
                expires_at,
                status_code,
            )
        except urllib.error.HTTPError as exc:
            status_code = int(exc.code)
            if status_code == 401:
                return CredentialCheck(
                    AccountStatus.EXPIRED.value,
                    "SSO 在线巡检返回 HTTP 401",
                    expires_at,
                    status_code,
                )
            if status_code == 429:
                return CredentialCheck(
                    AccountStatus.LIMITED.value,
                    "SSO 端点当前限流 (HTTP 429)",
                    expires_at,
                    status_code,
                )
            return CredentialCheck(
                AccountStatus.ERROR.value,
                "SSO 在线巡检 HTTP %s，可能被 Cloudflare 或代理拦截" % status_code,
                expires_at,
                status_code,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return CredentialCheck(
                AccountStatus.ERROR.value,
                "SSO 在线巡检失败: %s" % exc,
                expires_at,
                0,
            )

    def _inspect_cpa(self, account: Account, live: bool) -> CredentialCheck:
        if not account.access_token.strip():
            return CredentialCheck(AccountStatus.UNKNOWN.value, "未生成 CPA access token")
        expires = expiration_for(account)
        expires_at = iso_or_empty(expires)
        if expires is not None and expires <= datetime.now(timezone.utc) + self.expiry_skew:
            return CredentialCheck(
                AccountStatus.EXPIRED.value,
                "CPA access token 已于 %s 过期" % expires_at,
                expires_at,
            )
        if not live:
            if expires is None:
                return CredentialCheck(
                    AccountStatus.UNKNOWN.value,
                    "CPA token 没有可解析的到期时间，需要在线巡检",
                )
            return CredentialCheck(
                AccountStatus.ACTIVE.value,
                "CPA 本地有效期检查通过，到期时间 %s" % expires_at,
                expires_at,
            )
        return self._probe_cpa(account, expires_at)

    def _probe_cpa(self, account: Account, expires_at: str) -> CredentialCheck:
        base_url = self.cpa_base_url or DEFAULT_BASE_URL
        auth_payload = self._load_account_auth_payload(account)
        if auth_payload is not None:
            configured = str(auth_payload.get("base_url") or "").strip()
            if configured:
                base_url = configured.rstrip("/")
        hostname = str(urlparse(base_url).hostname or "").lower()
        if hostname in ("127.0.0.1", "localhost", "::1"):
            return self._inspect_local_cpa_hotload(account, expires_at)

        url = base_url.rstrip("/") + "/models"
        headers = dict(DEFAULT_HEADERS)
        headers["Authorization"] = "Bearer %s" % account.access_token.strip()
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self._opener().open(request, timeout=self.timeout_seconds) as response:
                status_code = int(getattr(response, "status", 200) or 200)
                body = json.loads(response.read().decode("utf-8", errors="replace"))
            models = [
                str(item.get("id"))
                for item in (body.get("data") or [])
                if isinstance(item, dict) and item.get("id")
            ]
            detail = "CPA 在线巡检通过"
            if models:
                detail += "，可用模型 %s" % ", ".join(models[:4])
            return CredentialCheck(AccountStatus.ACTIVE.value, detail, expires_at, status_code)
        except urllib.error.HTTPError as exc:
            status_code = int(exc.code)
            body = exc.read().decode("utf-8", errors="replace")[:300]
            if status_code in (401, 403):
                status = (
                    AccountStatus.EXPIRED.value
                    if status_code == 401
                    else AccountStatus.INVALID.value
                )
                detail = "CPA 在线巡检 HTTP %s，token 已失效: %s" % (status_code, body)
            elif status_code == 429:
                status = AccountStatus.LIMITED.value
                detail = "CPA token 有效但当前触发限流 (HTTP 429)"
            else:
                status = AccountStatus.ERROR.value
                detail = "CPA 在线巡检 HTTP %s: %s" % (status_code, body)
            return CredentialCheck(status, detail, expires_at, status_code)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            return CredentialCheck(
                AccountStatus.ERROR.value,
                "CPA 在线巡检失败: %s" % exc,
                expires_at,
                0,
            )

    def _load_account_auth_payload(self, account: Account) -> Optional[dict]:
        candidates: List[Path] = []
        if account.auth_file:
            candidates.append(Path(account.auth_file))
        hotload = self._resolve_local_hotload_file(account)
        if hotload is not None:
            candidates.append(hotload)
        seen: set[str] = set()
        for path in candidates:
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError, AttributeError):
                continue
            if isinstance(payload, dict):
                return payload
        return None

    def _resolve_local_hotload_file(self, account: Account) -> Optional[Path]:
        if self.cpa_hotload_dir is None:
            return None
        from grok_register.cpa_xai.schema import credential_file_name

        if account.auth_file:
            name = Path(account.auth_file).name
            if name:
                candidate = self.cpa_hotload_dir / name
                if candidate.is_file() or not account.email:
                    return candidate
        if account.email:
            return self.cpa_hotload_dir / credential_file_name(account.email)
        return None

    def _inspect_local_cpa_hotload(
        self,
        account: Account,
        expires_at: str,
    ) -> CredentialCheck:
        if self.cpa_hotload_dir is None:
            return CredentialCheck(
                AccountStatus.UNKNOWN.value,
                "CPA 指向本地服务，但未配置 cpa_hotload_dir",
                expires_at,
            )

        hotload_file = self._resolve_local_hotload_file(account)
        if hotload_file is None:
            return CredentialCheck(
                AccountStatus.INVALID.value,
                "账号没有可用于 CPA hotload 的 auth 文件",
                expires_at,
            )
        if not hotload_file.is_file():
            return CredentialCheck(
                AccountStatus.INVALID.value,
                "CPA hotload 文件尚未同步",
                expires_at,
            )
        try:
            payload = json.loads(hotload_file.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            return CredentialCheck(
                AccountStatus.ERROR.value,
                "CPA hotload 文件读取失败: %s" % exc,
                expires_at,
            )
        hotload_token = str(
            payload.get("access_token") if isinstance(payload, dict) else ""
        ).strip()
        if not hotload_token or hotload_token != account.access_token.strip():
            return CredentialCheck(
                AccountStatus.EXPIRED.value,
                "CPA hotload 与管理库 token 不一致，需要双向同步（以新为准）",
                expires_at,
            )
        detail = "CPA hotload 凭据已同步"
        if expires_at:
            detail += "，到期时间 %s" % expires_at
        return CredentialCheck(AccountStatus.ACTIVE.value, detail, expires_at)

    @staticmethod
    def _combine(
        account: Account,
        sso: CredentialCheck,
        cpa: CredentialCheck,
    ) -> tuple[str, str]:
        considered = [("SSO", sso)]
        if account.access_token.strip():
            considered.append(("CPA", cpa))
        detail = "；".join("%s: %s" % (name, result.detail) for name, result in considered)
        priority = (
            AccountStatus.NEEDS_LOGIN.value,
            AccountStatus.EXPIRED.value,
            AccountStatus.INVALID.value,
            AccountStatus.ERROR.value,
            AccountStatus.UNKNOWN.value,
            AccountStatus.LIMITED.value,
        )
        for status in priority:
            if any(result.status == status for _, result in considered):
                return status, detail
        return AccountStatus.ACTIVE.value, detail


ProgressCallback = Callable[[InspectionResult, int, int], None]


class InspectionService:
    def __init__(self, store: AccountStore, inspector: TokenInspector, max_workers: int = 6):
        self.store = store
        self.inspector = inspector
        self.max_workers = max(1, min(int(max_workers), 32))

    def inspect_accounts(
        self,
        account_ids: Iterable[int],
        live: bool = True,
        progress: Optional[ProgressCallback] = None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> List[InspectionResult]:
        accounts = self.store.get_many(list(account_ids))
        if not accounts:
            return []
        is_cancelled = cancelled or (lambda: False)
        results: List[InspectionResult] = []

        def inspect_one(account: Account) -> Optional[InspectionResult]:
            if is_cancelled():
                return None
            self.store.set_status(
                [account.id],
                AccountStatus.CHECKING.value,
                "正在巡检 SSO 与 CPA token",
            )
            return self.inspector.inspect(account, live)

        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(accounts))) as executor:
            futures: Dict[Future, Account] = {
                executor.submit(inspect_one, account): account
                for account in accounts
            }
            completed = 0
            for future in as_completed(futures):
                account = futures[future]
                try:
                    result = future.result()
                    if result is None:
                        continue
                except Exception as exc:
                    result = InspectionResult(
                        account_id=account.id,
                        status=AccountStatus.ERROR.value,
                        detail="巡检执行异常: %s" % exc,
                        checked_at=utc_now_iso(),
                        sso_status=AccountStatus.ERROR.value,
                        sso_detail="巡检执行异常",
                        cpa_status=AccountStatus.ERROR.value,
                        cpa_detail="巡检执行异常",
                    )
                self.store.apply_inspection(result)
                results.append(result)
                completed += 1
                if progress:
                    progress(result, completed, len(accounts))
        results.sort(key=lambda item: item.account_id)
        return results
