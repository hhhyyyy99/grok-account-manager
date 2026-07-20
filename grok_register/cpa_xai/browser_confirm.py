"""Approve xAI device-code in Chromium (DrissionPage).

Paths resolve relative to the grok_reg project root (parent of cpa_xai).

Proven flow (2026-07-10, free account; updated 2026-07-20):
  1. Attach SSO / login session
  2. Open grok.com and finish account TOS gate first:
     Cookie → tos-gate「知道了」
     (must complete before Grok Build OAuth, or SSO stays unusable)
  3. Open verification_uri_complete (user_code prefilled)
  4. Click 继续 on device page
  5. Cookie banner: 全部允许 (optional)
  6. Login with email / 使用邮箱登录 → fill email → 下一步
  7. Wait cf-turnstile-response → fill password → REAL click 登录
  8. May land /account redirect or device page → 继续
  9. Consent page /oauth2/device/consent → REAL click exact 允许
     (by_js click causes Invalid action / empty form action)
 10. /oauth2/device/done "设备已授权" + token poll SUCCESS

Hard rules:
  - Account TOS gate runs before Build OAuth allow
  - Token poll is source of truth
  - Button match is EXACT text only (允许 ≠ 全部允许)
  - Consent Allow MUST be a real click, not by_js
  - Prefer headed browser + register turnstilePatch
"""

from __future__ import annotations

import json
import os
import re
import struct
import sys
import threading
import time
from typing import Any, Callable

from grok_register.browser_cleanup import cleanup_browser_profile, quit_browser
from grok_register.paths import TURNSTILE_DIR
from urllib.parse import urlparse

LogFn = Callable[[str], None]

PASSWORD_SELECTOR = (
    "css:input[name='password'], input[data-testid='password'], input[type='password']"
)

TOS_GATE_MARKER = "tos-gate"
COOKIE_CONSENT_LABELS = (
    "接受所有 Cookie",
    "全部允许",
    "Accept All Cookies",
    "Accept all cookies",
    "Accept All",
    "Allow All",
)
TOS_GATE_LABELS = (
    "知道了",
    "Got it",
    "I understand",
    "I agree",
    "Agree",
    "Accept",
    "Continue",
    "继续",
    "同意",
)
CLOUDFLARE_MARKERS = (
    "just a moment",
    "checking your browser",
    "cf-challenge",
    "challenge-platform",
    "cdn-cgi/challenge",
    "attention required",
    "enable javascript and cookies",
    "verify you are human",
    "performing security verification",
    "needs to review the security",
    "ray id",
)
CLOUDFLARE_HARD_BLOCK_MARKERS = (
    "sorry, you have been blocked",
    "you are unable to access",
    "access denied",
    "error 1020",
    "error 1015",
    "why have i been blocked",
)
CLOUDFLARE_SPINNING_MARKERS = (
    "just a moment",
    "checking your browser",
    "performing security verification",
    "needs to review the security",
    "one more step",
    "verifying",
    "please wait",
    "正在验证",
    "请稍候",
    "安全验证",
)
GROK_APP_MARKERS = (
    "新建聊天",
    "new chat",
    "你想知道什么",
    "what do you want to know",
    "imagine",
    "automations",
    "私密模式",
    "private mode",
    "切换侧边栏",
    "ask grok",
)


def _noop_log(_: str) -> None:
    return None


class BrowserConfirmError(RuntimeError):
    pass


def _sleep(sec: float) -> None:
    time.sleep(sec)


def _build_mint_browser_options(
    *,
    headless: bool = False,
    log: LogFn | None = None,
):
    """Create isolated ChromiumOptions for CPA mint (unique port + profile)."""
    log = log or _noop_log
    from DrissionPage import ChromiumOptions

    opts = None
    try:
        from grok_register.app import create_browser_options  # type: ignore

        try:
            opts = create_browser_options(unique_profile=True, profile_tag="cpa")
        except TypeError:
            opts = create_browser_options()
        log("using register create_browser_options (turnstilePatch, isolated profile)")
    except Exception as e:  # noqa: BLE001
        log(f"register browser options unavailable: {e}")
        opts = None

    if opts is None:
        opts = ChromiumOptions()
        try:
            opts.set_timeouts(base=2)
        except Exception:
            pass
        for flag in (
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--mute-audio",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--window-size=1280,900",
        ):
            try:
                opts.set_argument(flag)
            except Exception:
                pass
        ext = str(TURNSTILE_DIR)
        if os.path.isdir(ext):
            try:
                opts.add_extension(ext)
                log(f"added extension {ext}")
            except Exception as e:  # noqa: BLE001
                log(f"extension add failed: {e}")

    for flag in (
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--mute-audio",
    ):
        try:
            opts.set_argument(flag)
        except Exception:
            pass

    if headless:
        try:
            opts.headless(True)
        except Exception:
            try:
                opts.set_argument("--headless=new")
            except Exception:
                pass
        log("headless=True (may hit Cloudflare / break real clicks)")
    else:
        try:
            opts.headless(False)
        except Exception:
            pass
        log(f"headed browser DISPLAY={os.environ.get('DISPLAY', '')!r}")

    for cand in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ):
        if os.path.isfile(cand):
            try:
                opts.set_browser_path(cand)
                log(f"browser path={cand}")
            except Exception:
                pass
            break
    # auto_port last — set_user_data_path / set_browser_path may clear it
    try:
        opts.auto_port()
    except Exception:
        pass
    return opts


def create_standalone_page(
    *,
    proxy: str | None = None,
    headless: bool = False,
    log: LogFn | None = None,
) -> tuple[Any, Any]:
    log = log or _noop_log
    try:
        from DrissionPage import Chromium
    except ImportError as e:
        raise BrowserConfirmError(
            "DrissionPage not installed; run inside grok_reg uv env or pip install DrissionPage"
        ) from e

    from .proxyutil import proxy_for_chromium, proxy_log_label, resolve_proxy

    proxy = resolve_proxy(proxy)
    chrome_proxy = proxy_for_chromium(proxy)
    last_err: BaseException | None = None

    for attempt in range(1, 5):
        opts = _build_mint_browser_options(headless=headless, log=log)
        if chrome_proxy:
            try:
                opts.set_argument(f"--proxy-server={chrome_proxy}")
            except Exception:
                pass
            log(f"browser proxy={proxy_log_label(proxy)} (chromium {chrome_proxy})")
        else:
            log("browser proxy=(none)")

        try:
            browser = Chromium(opts)
            page = browser.latest_tab
            if page is None:
                try:
                    page = browser.new_tab()
                except Exception:
                    page = None
            if page is None:
                raise BrowserConfirmError("standalone chromium started but page is None")
            log(f"standalone chromium started (attempt {attempt}/4)")
            return browser, page
        except Exception as e:  # noqa: BLE001
            last_err = e
            log(f"standalone chromium start failed attempt {attempt}/4: {e}")
            cleanup_browser_profile(opts)
            _sleep(min(1.2 * attempt, 4.0))

    raise BrowserConfirmError(f"standalone chromium start failed after retries: {last_err}")

def close_standalone(browser: Any) -> None:
    quit_browser(browser)


# ── mint browser reuse (per-thread) ──
_mint_tls = threading.local()


def _mint_tls_get() -> dict[str, Any]:
    d = getattr(_mint_tls, "state", None)
    if d is None:
        d = {"browser": None, "page": None, "served": 0, "proxy": None, "headless": None}
        _mint_tls.state = d
    return d


def clear_page_session(page: Any, browser: Any | None = None, log: LogFn | None = None) -> None:
    """Blank page + wipe storage/cookies for reuse between mint jobs."""
    log = log or _noop_log
    try:
        if page is not None:
            try:
                page.get("about:blank")
            except Exception:
                pass
            for js in (
                "try{localStorage.clear()}catch(e){}",
                "try{sessionStorage.clear()}catch(e){}",
            ):
                try:
                    page.run_js(js)
                except Exception:
                    pass
        for target in (page, browser):
            if target is None:
                continue
            try:
                target.set.cookies.clear()  # type: ignore[attr-defined]
                log("mint session cookies cleared")
                break
            except Exception:
                try:
                    # older API
                    cks = target.cookies()
                    if isinstance(cks, list):
                        for c in cks:
                            try:
                                target.set.cookies.remove(c)  # type: ignore[attr-defined]
                            except Exception:
                                pass
                except Exception:
                    pass
    except Exception as e:
        log(f"clear_page_session: {e}")


def normalize_cookies(cookies: Any) -> list[dict[str, Any]]:
    """Normalize DrissionPage / browser cookie list to settable dicts.

    Also clones SSO-like cookies onto accounts.x.ai / auth.x.ai domains so
    device-auth can skip secondary login when possible.
    """
    out: list[dict[str, Any]] = []
    if not cookies:
        return out
    if isinstance(cookies, dict):
        for k, v in cookies.items():
            if k and v is not None:
                out.append({"name": str(k), "value": str(v), "domain": ".x.ai", "path": "/"})
        cookies = out
        out = []
    if not isinstance(cookies, (list, tuple)):
        return out
    for c in cookies:
        if not isinstance(c, dict):
            continue
        name = c.get("name") or c.get("Name")
        value = c.get("value") or c.get("Value")
        if not name or value is None:
            continue
        domain = str(c.get("domain") or c.get("Domain") or ".x.ai")
        path = str(c.get("path") or c.get("Path") or "/")
        item = {
            "name": str(name),
            "value": str(value),
            "domain": domain,
            "path": path,
        }
        for src, dst in (
            ("expiry", "expiry"),
            ("expires", "expiry"),
            ("secure", "secure"),
            ("httpOnly", "httpOnly"),
            ("sameSite", "sameSite"),
        ):
            if src in c and c[src] is not None:
                item[dst] = c[src]
        out.append(item)

    # Expand SSO cookies to xAI account hosts (register browser is often on grok.com)
    sso_names = {"sso", "sso-rw", "cf_clearance", "sso_jwt", "__cf_bm"}
    extras: list[dict[str, Any]] = []
    seen = {(i["name"], i["domain"], i["path"]) for i in out}
    for item in list(out):
        n = item["name"]
        if n not in sso_names and not n.startswith("sso"):
            continue
        for dom in (".x.ai", "accounts.x.ai", ".accounts.x.ai", "auth.x.ai", ".auth.x.ai"):
            key = (n, dom, item["path"])
            if key in seen:
                continue
            clone = dict(item)
            clone["domain"] = dom
            extras.append(clone)
            seen.add(key)
    out.extend(extras)
    return out


def cookies_from_sso(sso: str) -> list[dict[str, Any]]:
    """Build multi-domain sso/sso-rw cookie clones for device-auth inject."""
    sso_val = str(sso or "").strip()
    if sso_val.startswith("sso="):
        sso_val = sso_val[4:].strip()
    if not sso_val:
        return []
    cookies: list[dict[str, Any]] = []
    for name in ("sso", "sso-rw"):
        for domain in (".x.ai", "accounts.x.ai", ".accounts.x.ai", "auth.x.ai"):
            cookies.append(
                {
                    "name": name,
                    "value": sso_val,
                    "domain": domain,
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                }
            )
    return cookies


def inject_cookies(page: Any, cookies: Any, log: LogFn | None = None) -> int:
    """Inject cookies into page/browser. Returns count attempted."""
    log = log or _noop_log
    items = normalize_cookies(cookies)
    if not items or page is None:
        return 0
    # Only seed accounts.x.ai. Visiting grok.com/auth.x.ai is slow and noisy
    # (heavy SPA / occasional 404-like redirects) before device auth.
    for url in (
        "https://accounts.x.ai/",
    ):
        try:
            page.get(url)
            _sleep(0.25)
        except Exception:
            continue

    n = 0
    for target_name, target in (("page", page), ("browser", getattr(page, "browser", None))):
        if target is None:
            continue
        try:
            target.set.cookies(items)  # type: ignore[attr-defined]
            n = len(items)
            log(f"injected cookies bulk via {target_name}={n}")
            break
        except Exception as e:
            log(f"bulk set via {target_name} failed: {e}")

    if n == 0:
        for item in items:
            ok = False
            for target in (page, getattr(page, "browser", None)):
                if target is None:
                    continue
                try:
                    target.set.cookies(item)  # type: ignore[attr-defined]
                    ok = True
                    break
                except Exception:
                    continue
            if ok:
                n += 1
        log(f"injected cookies one-by-one={n}/{len(items)}")

    # JS document.cookie for non-httpOnly SSO cookies (best effort)
    try:
        js_items = [
            c
            for c in items
            if (not c.get("httpOnly")) and c.get("name") in {"sso", "sso-rw", "cf_clearance"}
        ]
        if js_items:
            try:
                cur = str(getattr(page, "url", "") or "")
            except Exception:
                cur = ""
            if "accounts.x.ai" not in cur:
                page.get("https://accounts.x.ai/")
            for c in js_items:
                name = str(c["name"])
                val = str(c["value"])
                # avoid quote breakage
                if "'" in name or "'" in val:
                    continue
                page.run_js(
                    "document.cookie='"
                    + name
                    + "="
                    + val
                    + "; path=/; domain=.x.ai; Secure; SameSite=None'"
                )
            log(f"js cookie fallback applied={len(js_items)}")
    except Exception as e:
        log(f"js cookie fallback: {e}")

    return n


def acquire_mint_browser(

    *,
    proxy: str | None = None,
    headless: bool = False,
    reuse: bool = True,
    recycle_every: int = 15,
    log: LogFn | None = None,
) -> tuple[Any, Any, bool]:
    """Return (browser, page, owned). owned=True means caller must close if not reusing.

    When reuse=True, browser is kept in thread-local and cleared between jobs.
    """
    log = log or _noop_log
    st = _mint_tls_get()
    if reuse and st.get("browser") is not None:
        # recycle if proxy/headless changed or served enough
        need_recycle = (
            st.get("proxy") != (proxy or None)
            or st.get("headless") != headless
            or (recycle_every > 0 and int(st.get("served") or 0) >= recycle_every)
        )
        if not need_recycle:
            page = st.get("page")
            browser = st.get("browser")
            clear_page_session(page, browser, log=log)
            log(f"mint browser reused served={st.get('served')}")
            return browser, page, False
        log("mint browser recycle (proxy/headless/served threshold)")
        try:
            close_standalone(st.get("browser"))
        except Exception:
            pass
        st["browser"] = None
        st["page"] = None
        st["served"] = 0

    browser, page = create_standalone_page(proxy=proxy, headless=headless, log=log)
    if reuse:
        st["browser"] = browser
        st["page"] = page
        st["proxy"] = proxy or None
        st["headless"] = headless
        st["served"] = 0
        return browser, page, False
    return browser, page, True


def release_mint_browser(
    *,
    owned: bool,
    success: bool = True,
    force_quit: bool = False,
    log: LogFn | None = None,
) -> None:
    log = log or _noop_log
    st = _mint_tls_get()
    if force_quit or owned:
        browser = st.get("browser") if not owned else None
        # if owned, caller passes via closing create path — handle both
        if owned:
            # owned browser not in tls
            return
        if browser is not None:
            close_standalone(browser)
        st["browser"] = None
        st["page"] = None
        st["served"] = 0
        log("mint browser quit")
        return
    if success:
        st["served"] = int(st.get("served") or 0) + 1
    else:
        # fail: drop browser to avoid dirty state
        if st.get("browser") is not None:
            close_standalone(st.get("browser"))
            st["browser"] = None
            st["page"] = None
            st["served"] = 0
            log("mint browser dropped after failure")


def shutdown_mint_browsers() -> None:
    st = getattr(_mint_tls, "state", None)
    if not st:
        return
    if st.get("browser") is not None:
        close_standalone(st.get("browser"))
    st["browser"] = None
    st["page"] = None
    st["served"] = 0


def _page_url(page: Any) -> str:
    try:
        return page.url or ""
    except Exception:
        return ""


def _norm_probe_text(value: str) -> str:
    return " ".join(str(value or "").split()).casefold()


def looks_like_cloudflare(url: str = "", text: str = "") -> bool:
    blob = _norm_probe_text("%s %s" % (url, text))
    if not blob:
        return False
    return any(marker in blob for marker in CLOUDFLARE_MARKERS)


def looks_like_cloudflare_hard_block(url: str = "", text: str = "") -> bool:
    blob = _norm_probe_text("%s %s" % (url, text))
    if not blob:
        return False
    return any(marker in blob for marker in CLOUDFLARE_HARD_BLOCK_MARKERS)


def looks_like_cloudflare_spinning(url: str = "", text: str = "") -> bool:
    """True when CF is still checking/spinning, not a final hard block."""
    if looks_like_cloudflare_hard_block(url, text):
        return False
    blob = _norm_probe_text("%s %s" % (url, text))
    if not blob:
        return False
    if any(marker in blob for marker in CLOUDFLARE_SPINNING_MARKERS):
        return True
    # Managed challenge pages often only show CF chrome while the spinner runs.
    return looks_like_cloudflare(url, text) and (
        "cloudflare" in blob or "cf-" in blob or "challenge" in blob
    )


def looks_like_tos_gate(url: str = "", text: str = "") -> bool:
    url_l = str(url or "").casefold()
    text_l = _norm_probe_text(text)
    if TOS_GATE_MARKER in url_l:
        return True
    return any(
        marker.casefold() in text_l
        for marker in (
            "服务条款和可接受使用政策",
            "terms of service",
            "acceptable use policy",
            "知道了",
            "got it",
        )
    )


def looks_like_sign_in(url: str = "", text: str = "") -> bool:
    url_l = str(url or "").casefold()
    text_l = _norm_probe_text(text)
    if any(part in url_l for part in ("/sign-in", "/signin", "/login")):
        return True
    return any(
        marker in text_l
        for marker in (
            "使用邮箱登录",
            "continue with email",
            "sign in with email",
            "login with email",
        )
    )


def looks_like_grok_app(url: str = "", text: str = "") -> bool:
    url_l = str(url or "").casefold()
    text_l = _norm_probe_text(text)
    if "grok.com" not in url_l:
        return False
    if looks_like_cloudflare(url_l, text_l) or looks_like_tos_gate(url_l, text_l):
        return False
    if looks_like_sign_in(url_l, text_l):
        return False
    return any(marker.casefold() in text_l for marker in GROK_APP_MARKERS)


def _encode_grpc_tos_accepted() -> bytes:
    payload = struct.pack("B", (2 << 3) | 0) + struct.pack("B", 1)
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def _browser_post_binary(
    page: Any,
    *,
    url: str,
    data_hex: str,
    content_type: str,
    origin: str,
    referer: str,
) -> dict[str, Any]:
    payload = {
        "url": url,
        "dataHex": data_hex,
        "contentType": content_type,
        "origin": origin,
        "referer": referer,
    }
    script = (
        """
        const payload = %s;
        const bytes = new Uint8Array(
          payload.dataHex.match(/.{1,2}/g).map((b) => parseInt(b, 16))
        );
        return fetch(payload.url, {
          method: 'POST',
          credentials: 'include',
          headers: {
            'content-type': payload.contentType,
            'x-grpc-web': '1',
            'x-user-agent': 'connect-es/2.1.1',
            'origin': payload.origin,
            'referer': payload.referer,
          },
          body: bytes,
        }).then(async (response) => {
          const text = await response.text();
          return {
            status: response.status,
            body: (text || '').slice(0, 300),
          };
        }).catch((error) => ({ error: String(error) }));
        """
        % json.dumps(payload, ensure_ascii=False)
    )
    try:
        result = page.run_js(script)
    except Exception as exc:
        return {"error": str(exc)}
    return result if isinstance(result, dict) else {"error": "invalid browser binary post"}


def prepare_account_gates(
    page: Any,
    *,
    log: LogFn | None = None,
    timeout_sec: float = 60.0,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Finish grok.com TOS gate before Grok Build OAuth allow.

    Pass criteria (strict):
      - not stuck on Cloudflare challenge
      - not on sign-in
      - not on tos-gate
      - and either:
          * landed on grok app UI markers, or
          * previously saw/clicked TOS and then left tos-gate without CF/sign-in

    SetTosAcceptedVersion API is only auxiliary; it cannot alone mark success
    while the page still looks like CF/tos-gate/sign-in.
    """
    log = log or _noop_log
    if page is None:
        return {
            "ok": False,
            "tos_ok": False,
            "detail": "page is None",
        }

    def stopped() -> bool:
        return bool(stop_event is not None and stop_event.is_set())

    notes: list[str] = []
    cookie_ok = False
    clicked_tos = False
    saw_tos = False
    saw_cf = False
    api_ok = False
    try:
        log("prepare account TOS gate on grok.com before Build authorize")
        try:
            page.get("https://grok.com/")
        except TypeError:
            page.get("https://grok.com/")
        _sleep(2.0)

        deadline = time.time() + max(20.0, float(timeout_sec))
        while time.time() < deadline and not stopped():
            url = _page_url(page) or ""
            text = _visible_text(page) or ""
            url_l = url.casefold()
            text_l = _norm_probe_text(text)

            if looks_like_cloudflare(url, text):
                saw_cf = True
                if looks_like_cloudflare_hard_block(url, text):
                    notes.append("Cloudflare 硬拦截")
                    log("cloudflare hard block on grok.com during TOS gate")
                    break
                # Align with login turnstile budget (~45s), not a long gate-only wait.
                remaining = max(10.0, min(45.0, deadline - time.time()))
                cleared = bypass_cloudflare_challenge(
                    page,
                    log=log,
                    timeout_sec=remaining,
                    stop_event=stop_event,
                    reload_on_stuck=True,
                )
                if cleared:
                    notes.append("Cloudflare 已过盾")
                    _sleep(1.0)
                    continue
                notes.append("Cloudflare 过盾未完成")
                # keep looping until deadline; do not mark pass while CF remains
                _sleep(1.0)
                continue

            if looks_like_sign_in(url, text):
                notes.append("页面回到登录")
                break

            if any(label.casefold() in text_l for label in COOKIE_CONSENT_LABELS) or "cookie" in text_l:
                if _click_exact(page, list(COOKIE_CONSENT_LABELS), log, real=False):
                    cookie_ok = True
                    notes.append("Cookie 已同意")
                    _sleep(1.0)
                    continue

            if looks_like_tos_gate(url, text):
                saw_tos = True
                if _click_exact(page, list(TOS_GATE_LABELS), log, real=True):
                    clicked_tos = True
                    notes.append("已点击 TOS 确认")
                    _sleep(1.8)
                    continue
                _sleep(1.0)
                continue

            # Left tos-gate/sign-in/CF: only pass when app UI is visible, or we
            # already handled TOS and are no longer blocked.
            if looks_like_grok_app(url, text):
                notes.append("已进入 Grok 主界面")
                break
            if TOS_GATE_MARKER not in url_l and not looks_like_sign_in(url, text):
                if clicked_tos or saw_tos:
                    notes.append("已离开 TOS 门禁")
                    break
                # No gate and no app markers yet — keep waiting a bit for SPA.
            _sleep(1.0)

        final_url = _page_url(page) or ""
        final_text = _visible_text(page) or ""
        if looks_like_cloudflare(final_url, final_text):
            if looks_like_cloudflare_hard_block(final_url, final_text):
                detail = "；".join(notes + ["Cloudflare 硬拦截"])
                log("account TOS gate done ok=False detail=%s" % detail)
                return {"ok": False, "tos_ok": False, "detail": detail}
            log("final page still CF; one last bypass attempt")
            if bypass_cloudflare_challenge(
                page,
                log=log,
                timeout_sec=45.0,
                stop_event=stop_event,
                reload_on_stuck=False,
            ):
                notes.append("Cloudflare 最终过盾成功")
                final_url = _page_url(page) or ""
                final_text = _visible_text(page) or ""
            else:
                detail = "；".join(notes + ["Cloudflare 挑战未通过"])
                log("account TOS gate done ok=False detail=%s" % detail)
                return {"ok": False, "tos_ok": False, "detail": detail}
        if looks_like_sign_in(final_url, final_text):
            detail = "；".join(notes + ["SSO 无效或回到登录页"])
            log("account TOS gate done ok=False detail=%s" % detail)
            return {"ok": False, "tos_ok": False, "detail": detail}
        if looks_like_tos_gate(final_url, final_text):
            if _click_exact(page, list(TOS_GATE_LABELS), log, real=True):
                clicked_tos = True
                notes.append("TOS 门禁二次确认")
                _sleep(1.5)
                final_url = _page_url(page) or ""
                final_text = _visible_text(page) or ""
            if looks_like_cloudflare(final_url, final_text):
                detail = "；".join(notes + ["Cloudflare 挑战未通过"])
                log("account TOS gate done ok=False detail=%s" % detail)
                return {"ok": False, "tos_ok": False, "detail": detail}
            if looks_like_tos_gate(final_url, final_text):
                detail = "；".join(notes + ["仍停留在 tos-gate"])
                log("account TOS gate done ok=False detail=%s" % detail)
                return {"ok": False, "tos_ok": False, "detail": detail}

        app_ok = looks_like_grok_app(final_url, final_text)
        left_gate = (
            not looks_like_tos_gate(final_url, final_text)
            and not looks_like_cloudflare(final_url, final_text)
            and not looks_like_sign_in(final_url, final_text)
            and "grok.com" in final_url.casefold()
        )
        # Strict pass only when page is free of CF/tos/sign-in AND either:
        # - Grok app UI is visible, or
        # - we actually clicked TOS and then left the gate.
        # API success alone is never enough (avoids CF false pass).
        tos_ok = bool(left_gate and (app_ok or clicked_tos))
        if saw_cf and not tos_ok:
            notes.append("曾出现 Cloudflare 挑战")
        if not tos_ok and left_gate and not saw_tos and not app_ok:
            notes.append("未识别到主界面，且未出现/点击 TOS 门禁")
        if tos_ok and app_ok and "已进入 Grok 主界面" not in "；".join(notes):
            notes.append("已进入 Grok 主界面")
        if tos_ok and clicked_tos and "TOS 门禁已通过" not in "；".join(notes):
            notes.append("TOS 门禁已通过")

        # Auxiliary API accept: must run on accounts.x.ai, not while the tab is
        # still on grok.com (browser fetch is same-origin restricted / CORS).
        # Failures here must not override a successful UI gate pass.
        if tos_ok and not stopped():
            try:
                page.get("https://accounts.x.ai/account")
                _sleep(1.0)
                tos_api = _browser_post_binary(
                    page,
                    url="https://accounts.x.ai/auth_mgmt.AuthManagement/SetTosAcceptedVersion",
                    data_hex=_encode_grpc_tos_accepted().hex(),
                    content_type="application/grpc-web+proto",
                    origin="https://accounts.x.ai",
                    referer="https://accounts.x.ai/accept-tos",
                )
                status = int(tos_api.get("status") or 0)
                if 200 <= status < 300:
                    api_ok = True
                    notes.append("SetTosAcceptedVersion 成功")
                elif tos_api.get("error"):
                    log(
                        "SetTosAcceptedVersion auxiliary failed: %s"
                        % tos_api.get("error")
                    )
                else:
                    log("SetTosAcceptedVersion auxiliary HTTP %s" % status)
            except Exception as exc:
                log("SetTosAcceptedVersion auxiliary exception: %s" % exc)

        detail = "；".join(notes) if notes else ("账号授权完成" if tos_ok else "账号授权未确认")
        log("account TOS gate done ok=%s detail=%s" % (tos_ok, detail))
        return {
            "ok": bool(tos_ok),
            "tos_ok": bool(tos_ok),
            "detail": detail,
            "api_ok": bool(api_ok),
        }
    except Exception as exc:
        detail = "账号授权异常: %s" % exc
        log(detail)
        return {
            "ok": False,
            "tos_ok": False,
            "detail": detail,
        }


def _visible_text(page: Any) -> str:
    try:
        t = page.run_js(
            "return (document.body && (document.body.innerText || document.body.textContent)) || '';"
        )
        if isinstance(t, str) and t.strip():
            return t
    except Exception:
        pass
    try:
        raw = getattr(page, "raw_text", None)
        if callable(raw):
            t = raw()
            if isinstance(t, str) and t.strip():
                return t
        if isinstance(raw, str) and raw.strip():
            return raw
    except Exception:
        pass
    return ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _fill(
    page: Any,
    selector: str,
    value: str,
    log: LogFn,
    field_name: str,
) -> bool:
    """Clear and fill a form field without exposing its value in logs."""
    try:
        el = page.ele(selector, timeout=0.8)
    except Exception as e:
        log(f"find {field_name} input failed: {type(e).__name__}")
        return False
    if not el:
        log(f"{field_name} input not found")
        return False

    try:
        current = getattr(el, "value", None)
        if current is not None and str(current) == value:
            log(f"{field_name} already filled")
            return True
        el.clear(by_js=True)
        el.input(value)
        actual = getattr(el, "value", None)
        if actual is not None and str(actual) != value:
            log(f"fill {field_name} did not replace existing value")
            return False
    except Exception as e:
        log(f"fill {field_name} failed: {type(e).__name__}")
        return False
    log(f"filled {field_name}")
    return True


def _find_button_exact(page: Any, label: str) -> Any | None:
    try:
        for el in page.eles("tag:button") or []:
            try:
                if _norm(el.text or "") == label:
                    return el
            except Exception:
                continue
    except Exception:
        pass
    try:
        return page.ele(f"xpath://button[normalize-space(.)='{label}']", timeout=0.3)
    except Exception:
        return None


def _click_exact(
    page: Any,
    labels: list[str],
    log: LogFn,
    *,
    real: bool = False,
) -> str | None:
    """Click button by EXACT visible text. real=True uses physical click (needed for consent)."""
    for label in labels:
        el = _find_button_exact(page, label)
        if not el:
            continue
        try:
            if real:
                try:
                    el.scroll.to_see()
                except Exception:
                    pass
                el.click()
                log(f"clicked REAL exact {label!r}")
            else:
                el.click(by_js=True)
                log(f"clicked JS exact {label!r}")
            return label
        except Exception as e:
            log(f"click {label!r} failed: {e}")
            if real:
                try:
                    el.click(by_js=True)
                    log(f"clicked JS fallback exact {label!r}")
                    return label
                except Exception as e2:
                    log(f"js fallback {label!r} failed: {e2}")
    return None


def _click_email_login_chooser(
    page: Any, log: LogFn, visible_text: str = ""
) -> bool:
    """Choose email login, preferring xAI's stable test id over translated text."""
    labels = [
        "使用邮箱登录",
        "Login with email",
        "Continue with email",
        "Sign in with email",
    ]

    try:
        el = page.ele("css:button[data-testid='continue-with-email']", timeout=0.3)
    except Exception:
        el = None

    if el:
        try:
            el.click(by_js=True)
            log("clicked email login chooser by test id")
            return True
        except Exception as e:
            log(f"email login chooser test id click failed: {e}")

    if visible_text and not any(label in visible_text for label in labels):
        return False
    return _click_exact(page, labels, log, real=False) is not None


def _looks_like_wrong_password(visible_text: str) -> bool:
    text = str(visible_text or "")
    low = text.casefold()
    exact_markers = (
        "wrong email address or password",
        "incorrect email or password",
        "invalid email or password",
        "email or password is incorrect",
        "email or password you entered is incorrect",
        "the password you entered is incorrect",
        "incorrect password",
        "invalid credentials",
        "邮箱或密码错误",
        "邮箱地址或密码错误",
        "错误的邮箱地址或密码",
        "错误的邮箱或密码",
        "电子邮箱或密码不正确",
        "邮箱或密码不正确",
        "你输入的邮箱或密码不正确",
        "密码不正确",
        "密码错误",
    )
    # Only explicit credential-error copy may trigger auto password reset.
    # Do not use co-occurrence heuristics such as "password" + "invalid".
    return any(marker in low or marker in text for marker in exact_markers)


def _raise_for_login_error(visible_text: str) -> None:
    if _looks_like_wrong_password(visible_text):
        raise BrowserConfirmError("邮箱或密码错误")


def _turnstile_present(page: Any) -> bool:
    """Best-effort check that a Turnstile widget/token field exists on the page."""
    try:
        if page.ele("@name=cf-turnstile-response", timeout=0.15) is not None:
            return True
    except Exception:
        pass
    try:
        found = page.run_js(
            """
try {
  if (document.querySelector('input[name="cf-turnstile-response"]')) return true;
  if (document.querySelector('.cf-turnstile, iframe[src*="turnstile"], iframe[src*="challenges.cloudflare"]')) return true;
  if (window.turnstile) return true;
  return false;
} catch (e) { return false; }
            """
        )
        return bool(found)
    except Exception:
        return False


def _wait_turnstile(
    page: Any,
    log: LogFn,
    timeout: float = 45.0,
    stop_event: threading.Event | None = None,
) -> bool:
    """Wait/click Cloudflare Turnstile on the mint browser page.

    On slow networks the widget may never appear. Fail faster when:
    - stop_event is set / overall budget is exhausted
    - no Turnstile widget is observed for a short while
    """
    budget = max(3.0, float(timeout or 0.0))
    deadline = time.time() + budget
    no_widget_limit = min(12.0, max(5.0, budget * 0.45))
    no_widget_deadline = time.time() + no_widget_limit
    try:
        reset = page.run_js(
            """
if (window.turnstile && typeof window.turnstile.reset === 'function') {
  window.turnstile.reset();
  return true;
}
return false;
            """
        )
        if reset:
            log("turnstile reset")
    except Exception:
        pass

    clicked = False
    saw_widget = False
    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            log("turnstile wait interrupted by stop_event")
            return False
        if not saw_widget:
            saw_widget = _turnstile_present(page)
            if saw_widget:
                log("turnstile widget detected")
            elif time.time() >= no_widget_deadline:
                log(
                    "turnstile widget missing after %.0fs — likely network/CF stall"
                    % no_widget_limit
                )
                return False
        try:
            token = page.run_js(
                """
try {
  const input = document.querySelector('input[name="cf-turnstile-response"]');
  const byInput = String((input && input.value) || '').trim();
  if (byInput) return byInput;
  if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
    return String(window.turnstile.getResponse() || '').trim();
  }
  return '';
} catch (e) { return ''; }
                """
            )
            token = str(token or "").strip()
            if len(token) >= 80:
                log(f"turnstile ready len={len(token)}")
                return True
        except Exception:
            pass

        # Mimic register-machine: shadow-root checkbox click
        try:
            challenge_input = page.ele("@name=cf-turnstile-response", timeout=0.2)
            if challenge_input is not None:
                saw_widget = True
                wrapper = challenge_input.parent()
                iframe = None
                try:
                    iframe = wrapper.shadow_root.ele("tag:iframe")
                except Exception:
                    iframe = None
                if iframe is not None:
                    try:
                        iframe.run_js(
                            """
window.dtp = 1;
function getRandomInt(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }
let sx = getRandomInt(800, 1200);
let sy = getRandomInt(400, 700);
Object.defineProperty(MouseEvent.prototype, 'screenX', { value: sx });
Object.defineProperty(MouseEvent.prototype, 'screenY', { value: sy });
                            """
                        )
                    except Exception:
                        pass
                    try:
                        body_sr = iframe.ele("tag:body").shadow_root
                        btn = body_sr.ele("tag:input")
                        if btn is not None:
                            btn.click()
                            if not clicked:
                                log("clicked turnstile shadow checkbox")
                                clicked = True
                    except Exception:
                        pass
        except Exception:
            pass

        if not clicked:
            try:
                page.run_js(
                    """
const nodes = Array.from(document.querySelectorAll('div,span,iframe')).filter((n) => {
  const txt = (n.className || '') + ' ' + (n.id || '') + ' ' + (n.getAttribute?.('src') || '');
  return String(txt).toLowerCase().includes('turnstile');
});
if (nodes.length && typeof nodes[0].click === 'function') nodes[0].click();
                    """
                )
                clicked = True
                log("clicked turnstile container via JS")
            except Exception:
                pass
        _sleep(0.9)
    log("turnstile not ready")
    return False


def _click_cloudflare_widgets(page: Any, log: LogFn) -> bool:
    """Best-effort click on CF managed-challenge / turnstile widgets."""
    acted = False
    # Same shadow-root path as register/login turnstile wait.
    try:
        challenge_input = page.ele("@name=cf-turnstile-response", timeout=0.2)
        if challenge_input is not None:
            wrapper = challenge_input.parent()
            iframe = None
            try:
                iframe = wrapper.shadow_root.ele("tag:iframe")
            except Exception:
                iframe = None
            if iframe is not None:
                try:
                    iframe.run_js(
                        """
window.dtp = 1;
function getRandomInt(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }
let sx = getRandomInt(800, 1200);
let sy = getRandomInt(400, 700);
Object.defineProperty(MouseEvent.prototype, 'screenX', { value: sx });
Object.defineProperty(MouseEvent.prototype, 'screenY', { value: sy });
                        """
                    )
                except Exception:
                    pass
                try:
                    body_sr = iframe.ele("tag:body").shadow_root
                    btn = body_sr.ele("tag:input")
                    if btn is not None:
                        btn.click()
                        log("clicked CF turnstile checkbox")
                        acted = True
                except Exception:
                    pass
    except Exception:
        pass

    # Broader click targets used by managed challenges.
    try:
        clicked = page.run_js(
            """
const selectors = [
  'input[type=checkbox]',
  'label',
  'button',
  'div[role=button]',
  'iframe[src*="challenges.cloudflare"]',
  'iframe[src*="turnstile"]',
  '.cf-turnstile',
  '#challenge-stage',
  '#cf-stage',
];
const labels = ['Verify you are human', '确认您是真人', '继续', 'Continue'];
for (const sel of selectors) {
  for (const el of document.querySelectorAll(sel)) {
    try {
      const t = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
      const src = String(el.getAttribute?.('src') || '');
      if (
        labels.includes(t) ||
        src.includes('challenges.cloudflare') ||
        src.includes('turnstile') ||
        (el.type === 'checkbox')
      ) {
        el.click();
        return t || sel || src.slice(0, 40) || 'widget';
      }
    } catch (e) {}
  }
}
return null;
            """
        )
        if clicked:
            log("clicked CF widget %r" % clicked)
            acted = True
    except Exception as exc:
        log("CF widget click failed: %s" % exc)
    return acted


def _cf_turnstile_token(page: Any) -> str:
    try:
        token = page.run_js(
            """
try {
  const input = document.querySelector('input[name="cf-turnstile-response"]');
  const byInput = String((input && input.value) || '').trim();
  if (byInput) return byInput;
  if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
    return String(window.turnstile.getResponse() || '').trim();
  }
  return '';
} catch (e) { return ''; }
            """
        )
        return str(token or "").strip()
    except Exception:
        return ""


def _cf_has_interactive_widget(page: Any) -> bool:
    """True only when a real turnstile/checkbox widget is present (not just spinning text)."""
    try:
        found = page.run_js(
            """
try {
  if (document.querySelector('input[name="cf-turnstile-response"]')) return 'input';
  if (document.querySelector('iframe[src*="challenges.cloudflare.com"]')) return 'cf-iframe';
  if (document.querySelector('iframe[src*="turnstile"]')) return 'turnstile-iframe';
  if (document.querySelector('.cf-turnstile, [data-sitekey]')) return 'widget';
  return '';
} catch (e) { return ''; }
            """
        )
        return bool(str(found or "").strip())
    except Exception:
        return False


def _page_cf_clearance(page: Any) -> str:
    """Return cf_clearance cookie value if present for grok/cloudflare domains."""
    try:
        cookies = page.cookies() or []
    except Exception:
        return ""
    best = ""
    for cookie in cookies:
        if isinstance(cookie, dict):
            name = str(cookie.get("name") or "")
            value = str(cookie.get("value") or "")
            domain = str(cookie.get("domain") or "").casefold()
        else:
            name = str(getattr(cookie, "name", "") or "")
            value = str(getattr(cookie, "value", "") or "")
            domain = str(getattr(cookie, "domain", "") or "").casefold()
        if name != "cf_clearance" or not value:
            continue
        # Prefer grok.com clearance; fall back to any clearance seen.
        if "grok.com" in domain or domain.endswith(".grok.com") or not domain:
            return value
        if not best:
            best = value
    return best


def _reload_after_cf_clearance(
    page: Any,
    *,
    log: LogFn,
    target_url: str = "",
) -> bool:
    """Research finding: clearance often lands while UI stays on challenge page.

    Reload once so the browser re-requests grok.com with cf_clearance.
    Returns True when the page no longer looks like Cloudflare afterwards.
    """
    target = (target_url or _page_url(page) or "https://grok.com/").strip()
    if "grok.com" not in target.casefold():
        target = "https://grok.com/"
    log("cf_clearance present; reload %s to apply challenge pass" % target)
    try:
        page.get(target)
    except TypeError:
        page.get(target)
    except Exception as exc:
        log("cf_clearance reload failed: %s" % exc)
        return False
    _sleep(2.0)
    # SPA / challenge settle
    for _ in range(6):
        url = _page_url(page) or ""
        text = _visible_text(page) or ""
        if looks_like_cloudflare_hard_block(url, text):
            log("still hard-blocked after cf_clearance reload")
            return False
        if not looks_like_cloudflare(url, text):
            log("cloudflare cleared after cf_clearance reload")
            return True
        _sleep(1.0)
    url = _page_url(page) or ""
    text = _visible_text(page) or ""
    cleared = not looks_like_cloudflare(url, text)
    log("after cf_clearance reload cleared=%s url=%s" % (cleared, url[:120]))
    return cleared


def _click_visible_cf_checkbox(page: Any, log: LogFn) -> bool:
    """Click only a real CF checkbox / interactive widget. No blind container clicks."""
    # Shadow-root checkbox path (same as login, but only when input exists).
    try:
        challenge_input = page.ele("@name=cf-turnstile-response", timeout=0.2)
        if challenge_input is not None:
            wrapper = challenge_input.parent()
            iframe = None
            try:
                iframe = wrapper.shadow_root.ele("tag:iframe")
            except Exception:
                iframe = None
            if iframe is not None:
                try:
                    iframe.run_js(
                        """
window.dtp = 1;
function getRandomInt(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }
let sx = getRandomInt(800, 1200);
let sy = getRandomInt(400, 700);
Object.defineProperty(MouseEvent.prototype, 'screenX', { value: sx });
Object.defineProperty(MouseEvent.prototype, 'screenY', { value: sy });
                        """
                    )
                except Exception:
                    pass
                try:
                    body_sr = iframe.ele("tag:body").shadow_root
                    btn = body_sr.ele("tag:input")
                    if btn is not None:
                        btn.click()
                        log("clicked CF turnstile checkbox")
                        return True
                except Exception:
                    pass
    except Exception:
        pass

    # Visible labels only — no generic "nodes with turnstile in className" blind click.
    try:
        clicked = page.run_js(
            """
const labels = [
  'Verify you are human',
  '确认您是真人',
  'I am human',
  '我是真人',
];
for (const el of document.querySelectorAll('button, label, div[role=button], input[type=checkbox]')) {
  try {
    const t = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
    if (!t) continue;
    if (labels.includes(t) || (el.type === 'checkbox' && el.name !== 'cf-turnstile-response')) {
      el.click();
      return t || 'checkbox';
    }
  } catch (e) {}
}
return null;
            """
        )
        if clicked:
            log("clicked CF interactive control %r" % clicked)
            return True
    except Exception as exc:
        log("CF interactive click failed: %s" % exc)
    return False


def bypass_cloudflare_challenge(
    page: Any,
    *,
    log: LogFn | None = None,
    timeout_sec: float = 45.0,
    stop_event: threading.Event | None = None,
    reload_on_stuck: bool = True,
) -> bool:
    """Gate-side Cloudflare wait/bypass for grok.com Managed Challenge.

    Research findings:
      - grok.com often issues Managed Challenge, not password-page Turnstile
      - cf_clearance may appear while the UI still shows "请稍候/安全验证"
      - success path is: detect cf_clearance -> reload grok.com -> re-check page

    Rules:
      - do NOT reset turnstile
      - do NOT blind-click hidden turnstile containers
      - spinning/no-widget: wait
      - real checkbox/iframe: click once and settle
      - cf_clearance present: reload once to apply the pass
    """
    log = log or _noop_log
    if page is None:
        return False

    def stopped() -> bool:
        return bool(stop_event is not None and stop_event.is_set())

    start_url = _page_url(page) or "https://grok.com/"
    log("cloudflare challenge detected; gate mode (clearance+reload, no blind click)")
    # Same order of magnitude as login _wait_turnstile default (45s).
    deadline = time.time() + max(20.0, float(timeout_sec))
    reloaded = False
    clearance_reloaded = False
    last_progress = time.time()
    last_status_log = 0.0
    settle_rounds = 0
    clicked_once = False
    last_clearance = ""

    while time.time() < deadline and not stopped():
        url = _page_url(page) or ""
        text = _visible_text(page) or ""
        if not looks_like_cloudflare(url, text):
            log("cloudflare challenge cleared")
            return True

        if looks_like_cloudflare_hard_block(url, text):
            log("cloudflare hard block page detected; stop bypass early")
            return False

        spinning = looks_like_cloudflare_spinning(url, text)
        has_widget = _cf_has_interactive_widget(page)
        token = _cf_turnstile_token(page)
        clearance = _page_cf_clearance(page)
        now = time.time()
        if now - last_status_log >= 8.0:
            if clearance and spinning:
                state = "clearance-pending-reload"
            elif spinning and not has_widget:
                state = "spinning"
            elif has_widget:
                state = "interactive-widget"
            else:
                state = "challenge"
            log(
                "cloudflare still present (%s), wait %.0fs more"
                % (state, max(0.0, deadline - now))
            )
            last_status_log = now

        # Key fix: clearance often means challenge already passed server-side.
        if clearance and not clearance_reloaded:
            last_clearance = clearance
            clearance_reloaded = True
            last_progress = time.time()
            if _reload_after_cf_clearance(page, log=log, target_url=start_url):
                return True
            # Reload happened but page still looks like CF; keep waiting/clicking.
            continue

        # New clearance value after first reload attempt: try one more apply.
        if (
            clearance
            and clearance_reloaded
            and clearance != last_clearance
            and not reloaded
        ):
            last_clearance = clearance
            last_progress = time.time()
            if _reload_after_cf_clearance(page, log=log, target_url=start_url):
                return True
            continue

        # Token already filled (invisible challenge finished) — settle, then
        # reload if clearance is present.
        if len(token) >= 80:
            last_progress = time.time()
            log("turnstile token present len=%s; waiting page settle" % len(token))
            for _ in range(6):
                if stopped():
                    return False
                _sleep(1.0)
                url = _page_url(page) or ""
                text = _visible_text(page) or ""
                if not looks_like_cloudflare(url, text):
                    log("cloudflare cleared after turnstile token")
                    return True
                if looks_like_cloudflare_hard_block(url, text):
                    log("cloudflare hard block after turnstile token")
                    return False
                clearance = _page_cf_clearance(page)
                if clearance and not clearance_reloaded:
                    clearance_reloaded = True
                    last_clearance = clearance
                    if _reload_after_cf_clearance(page, log=log, target_url=start_url):
                        return True
                    break
            continue

        # Only click when a real widget is present.
        if has_widget:
            if _click_visible_cf_checkbox(page, log):
                clicked_once = True
                last_progress = time.time()
                for _ in range(10):
                    if stopped():
                        return False
                    _sleep(1.0)
                    url = _page_url(page) or ""
                    text = _visible_text(page) or ""
                    if not looks_like_cloudflare(url, text):
                        log("cloudflare cleared after widget click")
                        return True
                    if looks_like_cloudflare_hard_block(url, text):
                        log("cloudflare hard block after widget click")
                        return False
                    clearance = _page_cf_clearance(page)
                    if clearance and not clearance_reloaded:
                        clearance_reloaded = True
                        last_clearance = clearance
                        if _reload_after_cf_clearance(
                            page, log=log, target_url=start_url
                        ):
                            return True
                        break
                    if len(_cf_turnstile_token(page)) >= 80:
                        last_progress = time.time()
                    if looks_like_cloudflare_spinning(url, text):
                        last_progress = time.time()
                continue
            _sleep(1.5)
            continue

        # Spinning / no widget: wait only. No reset, no blind click.
        if spinning or not has_widget:
            settle_rounds += 1
            # If clearance appeared mid-wait, apply it immediately next loop.
            if clearance and not clearance_reloaded:
                continue
            last_progress = time.time()
            _sleep(2.0)
            continue

        settle_rounds += 1
        _sleep(2.0)
        quiet_for = time.time() - last_progress
        # Last-resort reload only after quiet stretch with no spin/widget/clearance.
        if (
            reload_on_stuck
            and not reloaded
            and not spinning
            and not has_widget
            and not clearance
            and quiet_for >= 12.0
            and (deadline - time.time()) >= 8.0
        ):
            reloaded = True
            log(
                "cloudflare quiet for %.0fs without widget/clearance; reload once"
                % quiet_for
            )
            try:
                target = start_url if "grok.com" in start_url.casefold() else "https://grok.com/"
                page.get(target)
            except Exception as exc:
                log("cloudflare reload failed: %s" % exc)
            _sleep(3.0)
            last_progress = time.time()

    # Final attempt: if clearance exists, one more reload before giving up.
    clearance = _page_cf_clearance(page)
    if clearance and not stopped():
        log("timeout with cf_clearance still present; final reload attempt")
        if _reload_after_cf_clearance(page, log=log, target_url=start_url):
            return True

    url = _page_url(page) or ""
    text = _visible_text(page) or ""
    cleared = not looks_like_cloudflare(url, text)
    if looks_like_cloudflare_hard_block(url, text):
        log("cloudflare bypass done cleared=False (hard block)")
        return False
    log(
        "cloudflare bypass done cleared=%s settle_rounds=%s reloaded=%s "
        "clearance_reloaded=%s clicked=%s has_clearance=%s"
        % (
            cleared,
            settle_rounds,
            reloaded,
            clearance_reloaded,
            clicked_once,
            bool(clearance or last_clearance),
        )
    )
    return cleared


def _prepare_password_login(
    page: Any,
    email: str,
    password: str,
    log: LogFn,
    timeout: float = 45.0,
    stop_event: threading.Event | None = None,
) -> bool:
    _fill(page, "css:input[type='email']", email, log, "email")
    if not _fill(
        page, PASSWORD_SELECTOR, password, log, "password"
    ):
        return False
    return _wait_turnstile(
        page,
        log,
        timeout=max(3.0, float(timeout or 0.0)),
        stop_event=stop_event,
    )


def approve_device_code(
    page: Any,
    *,
    verification_uri_complete: str,
    email: str,
    password: str = "",
    user_code: str = "",
    timeout_sec: float = 240.0,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
    allow_passwordless: bool = False,
    ensure_account_gates: bool = False,
    account_gates_state: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    log = log or _noop_log
    if page is None:
        raise BrowserConfirmError("page is None")
    email = (email or "").strip()
    password = password or ""
    if not email:
        raise BrowserConfirmError("email required")
    if not password and not allow_passwordless:
        raise BrowserConfirmError("email/password required")

    if not user_code and "user_code=" in (verification_uri_complete or ""):
        try:
            user_code = verification_uri_complete.split("user_code=", 1)[1].split("&", 1)[0]
        except Exception:
            user_code = ""

    log(f"open device url: {verification_uri_complete}")
    try:
        page.get(verification_uri_complete, timeout=60)
    except TypeError:
        page.get(verification_uri_complete)
    _sleep(1.0)

    deadline = time.time() + timeout_sec
    phase = "device"
    login_attempts = 0
    last_url = ""
    gates_done = bool(account_gates_state and account_gates_state.get("ok"))
    gate_state = dict(account_gates_state or {})

    def _run_account_gates_before_build() -> None:
        nonlocal gates_done, gate_state
        if gates_done or not ensure_account_gates:
            return
        log("login session ready — run TOS gate before Build authorize")
        remaining = max(20.0, deadline - time.time())
        gate_state = prepare_account_gates(
            page,
            log=log,
            timeout_sec=min(60.0, remaining),
            stop_event=stop_event,
        )
        gates_done = True
        if gate_state.get("ok"):
            log("account TOS ready before Build authorize: %s" % gate_state.get("detail"))
        else:
            log(
                "account TOS incomplete before Build authorize: %s"
                % (gate_state.get("detail") or "unknown")
            )
        # Resume device flow after grok.com detour.
        try:
            page.get(verification_uri_complete)
        except Exception as e:
            log(f"reopen device uri after account gates failed: {e}")
        _sleep(1.0)

    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            log("stop_event set — leave browser loop")
            return gate_state or None

        url = _page_url(page)
        text = _visible_text(page)
        if url != last_url:
            log(f"url: {url[:180]}")
            last_url = url
            snip = _norm(text)[:160]
            if snip:
                log(f"visible: {snip}")

        _raise_for_login_error(text)
        # Done page
        if "device/done" in url or "设备已授权" in text or "device authorized" in text.lower():
            log("device done page — waiting for token poll")
            _sleep(1.5)
            continue

        if "Invalid action" in text:
            log("Invalid action — reopen device uri")
            page.get(verification_uri_complete)
            _sleep(1.2)
            phase = "device"
            continue

        # xAI occasionally shows 404/not-found on stale device links or bad redirects
        low = (text or "").lower()
        if (
            "404" in (url or "")
            or "not found" in low
            or "页面不存在" in (text or "")
            or "找不到页面" in (text or "")
            or "this page could not be found" in low
        ):
            log(f"xAI 404/not-found on {(url or '')[:120]} — reopen device uri")
            try:
                page.get(verification_uri_complete)
            except Exception as e:
                log(f"reopen device uri failed: {e}")
            _sleep(1.2)
            phase = "device"
            continue

        # Consent page — REAL click exact 允许
        if "/consent" in url or "授权 Grok Build" in text or "Authorize Grok Build" in text:
            phase = "consent"
            # Account TOS must complete before Build OAuth allow, otherwise
            # the resulting SSO/session is still blocked by tos-gate.
            _run_account_gates_before_build()
            # Prefer real click; React needs it to set form action=allow
            if _click_exact(page, ["允许", "Allow", "Authorize", "Approve"], log, real=True):
                _sleep(2.5)
                continue
            # last resort: set action and submit
            try:
                page.run_js(
                    """
                    const f=document.querySelector('form');
                    if(!f) return;
                    let a=f.querySelector('input[name=action]');
                    if(!a){a=document.createElement('input');a.type='hidden';a.name='action';f.appendChild(a);}
                    a.value='allow';
                    const btn=[...f.querySelectorAll('button')].find(b=>((b.innerText||'').trim())==='允许'||(b.innerText||'').trim()==='Allow');
                    if(btn) btn.click(); else f.submit();
                    """
                )
                log("consent form submit via JS fallback")
                _sleep(2.5)
            except Exception as e:
                log(f"consent fallback failed: {e}")
            continue

        # Device code entry
        if page.ele("css:input[name='user_code']", timeout=0.3) and "consent" not in url:
            phase = "device"
            if user_code:
                try:
                    uc = page.ele("css:input[name='user_code']")
                    cur = (uc.value or "") if uc else ""
                    if user_code.replace("-", "") not in cur.replace("-", ""):
                        uc.clear()
                        uc.input(user_code)
                        log("filled user_code")
                except Exception:
                    pass
            if _click_exact(page, ["继续", "Continue"], log, real=False):
                _sleep(2.0)
                continue
            try:
                el = page.ele("css:button[type='submit']", timeout=0.5)
                if el:
                    el.click(by_js=True)
                    log("clicked device submit")
                    _sleep(2.0)
                    continue
            except Exception:
                pass

        # Account redirect
        if "正在重定向" in text or ("/account" in url and "sign-in" not in url):
            # Logged-in account page is a good moment to finish TOS before Build.
            if ensure_account_gates and not gates_done:
                _run_account_gates_before_build()
                continue
            if _click_exact(page, ["继续", "Continue"], log, real=False):
                _sleep(2.0)
                continue

        # Cookie banner (exact labels only)
        cookie_labels = [
            "全部允许",
            "全部拒绝",
            "Accept All Cookies",
            "Allow All",
            "Reject All",
        ]
        if any(label in text for label in cookie_labels) or "隐私偏好" in text:
            _click_exact(page, cookie_labels, log, real=False)
            _sleep(0.5)

        # Sign-in chooser
        if _click_email_login_chooser(page, log, text):
            if allow_passwordless and not password:
                raise BrowserConfirmError("SSO 会话不足，设备授权仍要求登录")
            _sleep(1.5)
            phase = "email"
            continue

        # Email only step
        if page.ele("css:input[type='email']", timeout=0.3) and not page.ele(
            PASSWORD_SELECTOR, timeout=0.2
        ):
            if allow_passwordless and not password:
                raise BrowserConfirmError("SSO 会话不足，设备授权仍要求登录")
            phase = "email"
            _fill(page, "css:input[type='email']", email, log, "email")
            if _click_exact(page, ["下一步", "Next", "Continue", "继续"], log, real=False):
                _sleep(1.8)
                continue

        # Password login
        if page.ele(PASSWORD_SELECTOR, timeout=0.3):
            if allow_passwordless and not password:
                raise BrowserConfirmError("SSO 会话不足，设备授权仍要求登录")
            phase = "password"
            remaining = max(0.0, deadline - time.time())
            if remaining < 8.0:
                _raise_for_login_error(_visible_text(page))
                raise BrowserConfirmError(
                    "浏览器登录未完成: phase=password remaining=%.0fs login_attempts=%s"
                    % (remaining, login_attempts)
                )
            if login_attempts >= 5:
                # Only auto-reset when the page explicitly reports bad credentials.
                _raise_for_login_error(_visible_text(page))
                raise BrowserConfirmError(
                    "密码页多次提交仍未通过（未检测到明确的邮箱或密码错误）"
                )
            login_attempts += 1
            log(f"login attempt {login_attempts}")
            # Cap per-attempt turnstile wait so a dead CF widget cannot burn the
            # whole browser_timeout, especially on slow/broken networks.
            turnstile_budget = min(20.0, max(6.0, remaining - 5.0))
            if not _prepare_password_login(
                page,
                email,
                password,
                log,
                timeout=turnstile_budget,
                stop_event=stop_event,
            ):
                if stop_event is not None and stop_event.is_set():
                    log("login interrupted while waiting for turnstile")
                    return gate_state or None
                log(
                    "login submit deferred: turnstile not ready "
                    "(budget=%.0fs remaining=%.0fs) — reloading sign-in"
                    % (turnstile_budget, remaining)
                )
                # Network stalls often leave a blank challenge slot above the
                # login button. Reload the password page instead of spinning.
                try:
                    page.get(verification_uri_complete)
                except Exception as e:
                    log(f"reload device uri after turnstile stall failed: {e}")
                _sleep(1.2)
                continue
            # REAL click login helps form submit
            if not _click_exact(page, ["登录", "Sign in", "Log in"], log, real=True):
                try:
                    el = page.ele("css:button[type='submit']", timeout=0.5) or page.ele(
                        "css:button[data-testid='sign-in-submit']", timeout=0.5
                    )
                    if el:
                        el.click()
                        log("clicked login submit real")
                except Exception as e:
                    log(f"login submit fail: {e}")
            # wait navigation / credential error — bound by remaining budget
            post_deadline = min(deadline, time.time() + 15.0)
            while time.time() < post_deadline:
                if stop_event is not None and stop_event.is_set():
                    return gate_state or None
                _sleep(0.5)
                current_text = _visible_text(page)
                _raise_for_login_error(current_text)
                if not page.ele(PASSWORD_SELECTOR, timeout=0.2):
                    break
                if "sign-in" not in _page_url(page):
                    break
            continue

        _sleep(1.0)

    if stop_event is not None and stop_event.is_set():
        log("browser finished via stop_event")
        return gate_state or None
    log(f"browser loop ended phase={phase} login_attempts={login_attempts}")
    # Never invent a wrong-password error for timeouts/stuck pages.
    _raise_for_login_error(_visible_text(page))
    raise BrowserConfirmError(
        "浏览器登录未完成: phase=%s login_attempts=%s" % (phase, login_attempts)
    )


def mint_with_browser(
    *,
    email: str,
    password: str = "",
    page: Any | None = None,
    proxy: str | None = None,
    headless: bool = False,
    browser_timeout_sec: float = 240.0,
    poll_log: LogFn | None = None,
    cancel: Callable[[], bool] | None = None,
    force_standalone: bool = True,
    cookies: Any | None = None,
    reuse_browser: bool = True,
    recycle_every: int = 15,
    allow_passwordless: bool = False,
    require_account_gates: bool = True,
) -> dict[str, Any]:
    """Request device code, approve in browser, poll tokens.

    force_standalone=True (default): do not reuse the *register* tab.
    Mint workers may still reuse their *own* Chromium via reuse_browser.
    cookies: optional register-browser cookie list to skip re-login.
    allow_passwordless: when True and cookies are injected, skip password gate;
    still fails if the browser shows a sign-in form.
    require_account_gates: when True, finish grok.com TOS gate before Build allow.
    """
    from .oauth_device import OAuthDeviceError, poll_device_token, request_device_code
    from .proxyutil import proxy_log_label, resolve_proxy, set_runtime_proxy

    log = poll_log or _noop_log
    require_account_gates = bool(require_account_gates)
    own_browser = None
    owned = False
    work_page = None if force_standalone else page
    resolved = resolve_proxy(proxy)
    set_runtime_proxy(resolved or None)
    success = False
    try:
        last_err: BaseException | None = None
        sess = None
        for attempt in range(1, 4):
            try:
                sess = request_device_code(proxy=resolved or None)
                last_err = None
                break
            except BaseException as e:  # noqa: BLE001
                last_err = e
                log(f"request_device_code attempt {attempt}/3 failed: {e}")
                _sleep(1.5 * attempt)
        if sess is None:
            raise last_err or RuntimeError("request_device_code failed")
        log(
            f"device user_code={sess.user_code} expires_in={sess.expires_in} "
            f"proxy={proxy_log_label(resolved) or '(none)'}"
        )

        if work_page is None:
            own_browser, work_page, owned = acquire_mint_browser(
                proxy=resolved or None,
                headless=headless,
                reuse=reuse_browser,
                recycle_every=recycle_every,
                log=log,
            )
            if owned:
                # non-reuse path: track for finally close
                pass

        # Cookie inject before opening device URL (skip secondary login when possible)
        if cookies:
            n = inject_cookies(work_page, cookies, log=log)
            log(f"cookie inject count={n}")
            try:
                url = _page_url(work_page)
                if "accounts.x.ai" not in (url or ""):
                    work_page.get("https://accounts.x.ai/")
                    _sleep(0.4)
                url = _page_url(work_page)
                visible = _visible_text(work_page)
                snip = _norm(visible)[:120]
                log(f"post-inject session url={url[:120]} visible={snip}")
            except Exception as e:
                log(f"post-inject check: {e}")
            if require_account_gates:
                # With an existing SSO session, finish TOS before Build OAuth.
                pre_gate = prepare_account_gates(
                    work_page,
                    log=log,
                    timeout_sec=min(60.0, float(browser_timeout_sec)),
                )
                if not pre_gate.get("ok"):
                    log(
                        "account TOS incomplete before Build authorize: %s"
                        % (pre_gate.get("detail") or "unknown")
                    )
                else:
                    log(
                        "account TOS ready before Build authorize: %s"
                        % (pre_gate.get("detail") or "ok")
                    )
            else:
                pre_gate = {
                    "ok": True,
                    "tos_ok": False,
                    "detail": "skipped-account-gates",
                }
                log("account TOS gate skipped (require_account_gates=false)")
        else:
            # Password login path can finish TOS inside approve_device_code,
            # after sign-in and before clicking Build「允许」.
            pre_gate = {
                "ok": False,
                "tos_ok": False,
                "detail": (
                    "deferred-until-login"
                    if require_account_gates
                    else "skipped-account-gates"
                ),
            }
            if not require_account_gates:
                log("account TOS gate skipped (require_account_gates=false)")

        if cancel and cancel():
            raise BrowserConfirmError("cancelled before Build authorize")

        stop_event = threading.Event()
        token_box: dict[str, Any] = {}
        err_box: dict[str, BaseException] = {}
        browser_error: BrowserConfirmError | None = None

        def _poll_cancel() -> bool:
            return stop_event.is_set() or bool(cancel and cancel())

        def _poll() -> None:
            try:
                time.sleep(1)
                tr = poll_device_token(
                    sess.device_code,
                    interval=max(sess.interval, 5),
                    expires_in=min(sess.expires_in, int(browser_timeout_sec) + 60),
                    log=log,
                    cancel=_poll_cancel,
                    proxy=resolved or None,
                )
                token_box["token"] = tr
                stop_event.set()
                log("token poll SUCCESS — stop_event set")
            except BaseException as e:  # noqa: BLE001
                err_box["err"] = e
                stop_event.set()

        t = threading.Thread(target=_poll, name="oauth-poll", daemon=True)
        t.start()
        try:
            gate_result = approve_device_code(
                work_page,
                verification_uri_complete=sess.verification_uri_complete,
                email=email,
                password=password,
                user_code=sess.user_code,
                timeout_sec=browser_timeout_sec,
                stop_event=stop_event,
                log=log,
                allow_passwordless=allow_passwordless,
                # Cookie path may have already finished; password path does it here
                # only when the caller asked for account gates.
                ensure_account_gates=require_account_gates,
                account_gates_state=pre_gate if cookies else None,
            )
            if isinstance(gate_result, dict) and gate_result.get("detail"):
                pre_gate = gate_result
        except BrowserConfirmError as e:
            browser_error = e
            stop_event.set()
            log(f"browser confirm failed: {e}")

        t.join(timeout=max(browser_timeout_sec, 60) + 30)
        if "token" in token_box:
            tr = token_box["token"]
            success = True
            return {
                "access_token": tr.access_token,
                "refresh_token": tr.refresh_token,
                "id_token": tr.id_token,
                "token_type": tr.token_type,
                "expires_in": tr.expires_in,
                "user_code": sess.user_code,
                "account_gates": pre_gate,
            }
        if browser_error is not None:
            raise browser_error
        if "err" in err_box:
            raise err_box["err"]
        raise OAuthDeviceError("token poll thread ended without result")
    finally:
        if own_browser is not None:
            if owned:
                close_standalone(own_browser)
            else:
                release_mint_browser(owned=False, success=success, log=log)
