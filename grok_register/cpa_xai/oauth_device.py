"""xAI OAuth device-code grant (Grok CLI / CPA client).

Endpoints from https://auth.x.ai/.well-known/openid-configuration
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .proxyutil import resolve_proxy

# Keep in sync with CLIProxyAPI internal/auth/xai/types.go
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
ISSUER = "https://auth.x.ai"
DEVICE_CODE_URL = "https://auth.x.ai/oauth2/device/code"
TOKEN_URL = "https://auth.x.ai/oauth2/token"
SCOPE = "openid profile email offline_access grok-cli:access api:access"

LogFn = Callable[[str], None]


def _noop_log(_: str) -> None:
    return None


def _proxy_handler(proxy: str | None = None) -> urllib.request.ProxyHandler | None:
    p = resolve_proxy(proxy)
    if not p:
        return None
    return urllib.request.ProxyHandler({"http": p, "https": p})


def _opener(proxy: str | None = None) -> urllib.request.OpenerDirector:
    handlers: list[Any] = []
    ph = _proxy_handler(proxy)
    if ph is not None:
        handlers.append(ph)
    return urllib.request.build_opener(*handlers) if handlers else urllib.request.build_opener()


def _post_form(
    url: str,
    form: dict[str, str],
    timeout: float = 30.0,
    *,
    proxy: str | None = None,
) -> tuple[int, dict[str, Any] | str]:
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "grok-reg-cpa-xai-minter/1.0",
        },
    )
    opener = _opener(proxy)
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = getattr(resp, "status", 200) or 200
            try:
                return int(status), json.loads(body)
            except json.JSONDecodeError:
                return int(status), body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return int(e.code), json.loads(body)
        except json.JSONDecodeError:
            return int(e.code), body


@dataclass
class DeviceCodeSession:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int
    raw: dict[str, Any]


@dataclass
class TokenResult:
    access_token: str
    refresh_token: str
    id_token: str | None
    token_type: str
    expires_in: int
    raw: dict[str, Any]


class OAuthDeviceError(RuntimeError):
    """OAuth device/token errors.

    retryable=True means the failure is transient (network/timeout/proxy) and
    must not permanently invalidate a refresh_token.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = bool(retryable)


_SENSITIVE_BODY_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "device_code",
        "user_code",
        "client_secret",
        "password",
        "sso",
        "token",
    }
)


def _is_sensitive_oauth_key(key: str) -> bool:
    lowered = re.sub(r"[\s_\-]+", "", str(key or "").lower())
    if not lowered:
        return False
    if lowered in {
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "devicecode",
        "usercode",
        "clientsecret",
        "password",
        "sso",
        "token",
    }:
        return True
    return "token" in lowered or "secret" in lowered or "password" in lowered


def _redact_oauth_value(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_sensitive_oauth_key(key_text):
                redacted[key_text] = "***"
            else:
                redacted[key_text] = _redact_oauth_value(item)
        return redacted
    if isinstance(value, list):
        return [_redact_oauth_value(item) for item in value]
    if isinstance(value, str):
        return _redact_oauth_text(value)
    return value


def _redact_oauth_text(text: str) -> str:
    """Redact secrets that leak into free-form OAuth error strings."""
    value = str(text or "")
    if not value:
        return value
    # snake_case, camelCase, kebab-case, quoted JSON, Bearer, JWT.
    patterns = (
        r"(?i)\b((?:access|refresh|id)[_-]?token|device[_-]?code|user[_-]?code|client[_-]?secret|password|sso)\s*[:=]\s*([^\s,;]+)",
        r"(?i)\b((?:access|refresh|id)Token|deviceCode|userCode|clientSecret)\s*[:=]\s*([^\s,;]+)",
        r'(?i)("(?:access[_-]?token|refresh[_-]?token|id[_-]?token|device[_-]?code|client[_-]?secret|password|accessToken|refreshToken|idToken)"\s*:\s*")([^"]+)(")',
        r"(?i)\b(bearer)\s+([A-Za-z0-9\-._~+/]+=*)",
    )
    redacted = value
    redacted = re.sub(patterns[0], r"\1=***", redacted)
    redacted = re.sub(patterns[1], r"\1=***", redacted)
    redacted = re.sub(patterns[2], r"\1***\3", redacted)
    redacted = re.sub(patterns[3], r"\1 ***", redacted)
    if redacted.count(".") >= 2 and len(redacted) > 40 and " " not in redacted.strip():
        return "***"
    redacted = re.sub(
        r"\beyJ[A-Za-z0-9_\-]+=*\.[A-Za-z0-9_\-]+=*\.[A-Za-z0-9_\-+=]*\b",
        "***",
        redacted,
    )
    return redacted


def _format_oauth_body(body: Any) -> str:
    return repr(_redact_oauth_value(body))


def _format_oauth_text(value: Any) -> str:
    """Format any error/error_description payload with recursive redaction."""
    if isinstance(value, (dict, list)):
        return _format_oauth_body(value)
    return _redact_oauth_text(str(value or ""))


def request_device_code(
    *,
    client_id: str = CLIENT_ID,
    scope: str = SCOPE,
    timeout: float = 30.0,
    proxy: str | None = None,
) -> DeviceCodeSession:
    status, body = _post_form(
        DEVICE_CODE_URL,
        {"client_id": client_id, "scope": scope},
        timeout=timeout,
        proxy=proxy,
    )
    if status != 200 or not isinstance(body, dict):
        raise OAuthDeviceError(
            f"device code request failed HTTP {status}: {_format_oauth_body(body)}"
        )
    device_code = str(body.get("device_code") or "").strip()
    user_code = str(body.get("user_code") or "").strip()
    if not device_code or not user_code:
        raise OAuthDeviceError(
            f"device code response missing fields: {_format_oauth_body(body)}"
        )
    vuri = str(body.get("verification_uri") or "https://accounts.x.ai/oauth2/device").strip()
    vcomplete = str(
        body.get("verification_uri_complete") or f"{vuri}?user_code={user_code}"
    ).strip()
    expires_in = int(body.get("expires_in") or 1800)
    interval = max(int(body.get("interval") or 5), 1)
    return DeviceCodeSession(
        device_code=device_code,
        user_code=user_code,
        verification_uri=vuri,
        verification_uri_complete=vcomplete,
        expires_in=expires_in,
        interval=interval,
        raw=body,
    )


def _token_result_from_body(
    body: dict[str, Any],
    *,
    fallback_refresh_token: str = "",
) -> TokenResult:
    access = str(body.get("access_token") or "").strip()
    if not access:
        raise OAuthDeviceError(
            f"token response missing access_token: {_format_oauth_body(body)}"
        )
    refresh = str(body.get("refresh_token") or fallback_refresh_token or "").strip()
    if not refresh:
        raise OAuthDeviceError("token response missing refresh_token")
    return TokenResult(
        access_token=access,
        refresh_token=refresh,
        id_token=(str(body["id_token"]).strip() if body.get("id_token") else None),
        token_type=str(body.get("token_type") or "Bearer"),
        expires_in=int(body.get("expires_in") or 21600),
        raw=body,
    )


def poll_device_token(
    device_code: str,
    *,
    client_id: str = CLIENT_ID,
    interval: int = 5,
    expires_in: int = 1800,
    timeout: float = 30.0,
    log: LogFn | None = None,
    cancel: Callable[[], bool] | None = None,
    proxy: str | None = None,
) -> TokenResult:
    """Poll token endpoint until authorized or expired."""
    log = log or _noop_log
    deadline = time.time() + max(expires_in - 5, 30)
    sleep_for = max(interval, 1)
    while time.time() < deadline:
        if cancel and cancel():
            raise OAuthDeviceError("cancelled")
        try:
            status, body = _post_form(
                TOKEN_URL,
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device_code,
                    "client_id": client_id,
                },
                timeout=timeout,
                proxy=proxy,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log(f"oauth poll network error: {type(e).__name__} (sleep {sleep_for}s)")
            time.sleep(sleep_for)
            continue
        if status == 200 and isinstance(body, dict) and body.get("access_token"):
            return _token_result_from_body(body)
        err = ""
        desc = ""
        err_display = ""
        if isinstance(body, dict):
            raw_err = body.get("error")
            # Keep raw string codes for control flow; only redacted text leaves.
            err = str(raw_err or "") if not isinstance(raw_err, (dict, list)) else ""
            err_display = _format_oauth_text(raw_err)
            desc = _format_oauth_text(body.get("error_description") or "")
        if err in ("authorization_pending", "slow_down"):
            if err == "slow_down":
                sleep_for = min(sleep_for + 5, 30)
            log(f"oauth poll: {err} (sleep {sleep_for}s)")
            time.sleep(sleep_for)
            continue
        if err in ("expired_token", "access_denied"):
            raise OAuthDeviceError(f"device auth failed: {err_display}: {desc}")
        if status == 400 and (err or err_display):
            raise OAuthDeviceError(
                f"device auth token error: {err_display}: {desc or _format_oauth_body(body)}"
            )
        log(f"oauth poll unexpected HTTP {status}: {_format_oauth_body(body)}")
        time.sleep(sleep_for)
    raise OAuthDeviceError("device auth timed out waiting for user approval")


def refresh_access_token(
    refresh_token: str,
    *,
    client_id: str = CLIENT_ID,
    timeout: float = 30.0,
    proxy: str | None = None,
) -> TokenResult:
    """Exchange a refresh_token for a new access_token (and possibly rotated refresh)."""
    refresh_token = (refresh_token or "").strip()
    if not refresh_token:
        raise OAuthDeviceError("refresh_token is required")
    try:
        status, body = _post_form(
            TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
            timeout=timeout,
            proxy=proxy,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise OAuthDeviceError(
            f"refresh token network error: {type(e).__name__}: {e}",
            retryable=True,
        ) from e
    if status == 200 and isinstance(body, dict):
        return _token_result_from_body(body, fallback_refresh_token=refresh_token)
    err = ""
    desc = ""
    err_display = ""
    if isinstance(body, dict):
        raw_err = body.get("error")
        err = str(raw_err or "") if not isinstance(raw_err, (dict, list)) else ""
        err_display = _format_oauth_text(raw_err)
        desc = _format_oauth_text(body.get("error_description") or "")
    if err or err_display or status >= 400:
        # 5xx and transport-adjacent failures are transient; 4xx grant errors are not.
        retryable = status >= 500 or status == 429
        raise OAuthDeviceError(
            f"refresh token failed HTTP {status}: {err_display or _format_oauth_body(body)}"
            + (f": {desc}" if desc else ""),
            retryable=retryable,
        )
    raise OAuthDeviceError(
        f"refresh token unexpected response HTTP {status}: {_format_oauth_body(body)}",
        retryable=True,
    )
