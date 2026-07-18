"""Worker executed with the registration runtime embedded in this project.

Browser automation and DrissionPage are loaded only in this subprocess so the
local management server can still start and report missing automation packages.
"""

from __future__ import annotations

import sys
import argparse
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List


OUTPUT_LOCK = threading.Lock()


def emit(prefix: str, payload: Dict[str, Any]) -> None:
    with OUTPUT_LOCK:
        print(prefix + json.dumps(payload, ensure_ascii=False), flush=True)


def current_sso_cookie() -> str:
    """Read the SSO cookie left by mint_with_browser in this worker thread."""
    try:
        from grok_register.cpa_xai import browser_confirm

        get_state = getattr(browser_confirm, "_mint_tls_get", None)
        if not callable(get_state):
            return ""
        page = (get_state() or {}).get("page")
        if page is None:
            return ""
        cookies = page.cookies(all_domains=True, all_info=True) or []
        by_name: Dict[str, str] = {}
        for item in cookies:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                value = str(item.get("value") or "").strip()
            else:
                name = str(getattr(item, "name", "") or "").strip()
                value = str(getattr(item, "value", "") or "").strip()
            if name and value:
                by_name[name] = value
        return by_name.get("sso") or by_name.get("sso-rw") or ""
    except Exception:
        return ""


def run_password_reset_item(
    item: Dict[str, Any], settings: Dict[str, Any]
) -> Dict[str, Any]:
    account_id = int(item.get("id") or 0)
    email = str(item.get("email") or "").strip()

    def log(message: str) -> None:
        emit("GM_LOG ", {"id": account_id, "email": email, "message": str(message)})

    log("开始读取重置前邮件")
    try:
        from grok_register.password_reset import reset_password

        result = reset_password(
            email=email,
            mail_credential=str(item.get("mail_credential") or ""),
            proxy=str(settings.get("proxy") or "") or None,
            headless=bool(settings.get("headless", False)),
            timeout_seconds=float(settings.get("timeout_seconds") or 300),
            log=log,
        )
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
        log("密码重置失败: %s" % exc)
    result["id"] = account_id
    result["email"] = email
    emit("GM_RESULT ", result)
    return result

def run_login_item(item: Dict[str, Any], settings: Dict[str, Any], mint_and_export) -> Dict[str, Any]:
    account_id = int(item.get("id") or 0)
    email = str(item.get("email") or "").strip()

    def log(message: str) -> None:
        emit("GM_LOG ", {"id": account_id, "email": email, "message": str(message)})

    result = mint_and_export(
        email=email,
        password=str(item.get("password") or ""),
        auth_dir=str(item.get("auth_dir") or settings["default_auth_dir"]),
        proxy=str(settings.get("proxy") or "") or None,
        headless=bool(settings.get("headless", False)),
        base_url=str(settings.get("base_url") or "https://cli-chat-proxy.grok.com/v1"),
        probe=bool(settings.get("probe", False)),
        probe_chat=False,
        browser_timeout_sec=float(settings.get("timeout_seconds") or 300),
        force_standalone=True,
        reuse_browser=bool(settings.get("reuse_browser", True)),
        recycle_every=max(1, int(settings.get("recycle_every") or 10)),
        log=log,
    )
    if result.get("ok"):
        sso_token = current_sso_cookie()
        result["sso_token"] = sso_token
        result["sso_refreshed"] = bool(sso_token)
        if sso_token:
            log("fresh sso cookie captured")
        else:
            log("OAuth token refreshed but sso cookie was not found")
    result["id"] = account_id
    result["email"] = email
    emit("GM_RESULT ", result)
    return result


def run_login_batch(
    items: List[Dict[str, Any]],
    settings: Dict[str, Any],
    mint_and_export,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    try:
        for item in items:
            results.append(run_login_item(item, settings, mint_and_export))
        return results
    finally:
        try:
            from grok_register.cpa_xai.browser_confirm import shutdown_mint_browsers

            shutdown_mint_browsers()
        except Exception:
            pass


def _read_input_document(input_path: str) -> Dict[str, Any]:
    if str(input_path) == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(str(input_path)).read_text(encoding="utf-8")
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError("worker 输入必须是 JSON 对象")
    return document
def batch_login(args: argparse.Namespace) -> int:
    try:
        from grok_register.cpa_xai.mint import mint_and_export
    except Exception as exc:
        emit("GM_FATAL ", {"error": "无法加载内置登录模块: %s" % exc})
        return 3

    try:
        document = _read_input_document(args.input)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        emit("GM_FATAL ", {"error": "批量登录输入读取失败: %s" % exc})
        return 3
    accounts = document.get("accounts") or []
    settings = document.get("settings") or {}
    if not isinstance(accounts, list) or not accounts:
        emit("GM_FATAL ", {"error": "批量登录没有账号"})
        return 3

    workers = max(1, min(int(settings.get("workers") or 1), 10, len(accounts)))
    batches = [accounts[index::workers] for index in range(workers)]
    batches = [batch for batch in batches if batch]
    failures = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(run_login_batch, batch, settings, mint_and_export)
            for batch in batches
        ]
        for future in as_completed(futures):
            try:
                results = future.result()
                failures += sum(1 for result in results if not result.get("ok"))
            except Exception as exc:
                failures += 1
                emit("GM_FATAL ", {"error": "登录工作线程异常: %s" % exc})
    return 0 if failures == 0 else 2


def batch_password_reset(args: argparse.Namespace) -> int:
    try:
        document = _read_input_document(args.input)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        emit("GM_FATAL ", {"error": "密码重置输入读取失败: %s" % exc})
        return 3
    accounts = document.get("accounts") or []
    settings = document.get("settings") or {}
    if not isinstance(accounts, list) or not accounts:
        emit("GM_FATAL ", {"error": "密码重置没有账号"})
        return 3

    workers = max(1, min(int(settings.get("workers") or 1), 10, len(accounts)))
    failures = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(run_password_reset_item, item, settings)
            for item in accounts
        ]
        for future in as_completed(futures):
            try:
                result = future.result()
                failures += int(not result.get("ok"))
            except Exception as exc:
                failures += 1
                emit("GM_FATAL ", {"error": "密码重置工作线程异常: %s" % exc})
    return 0 if failures == 0 else 2

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="grok-manager reference browser worker")
    subparsers = parser.add_subparsers(dest="command", required=True)
    login = subparsers.add_parser("batch-login")
    login.add_argument("--input", required=True)
    login.set_defaults(handler=batch_login)
    reset = subparsers.add_parser("reset-password")
    reset.add_argument("--input", required=True)
    reset.set_defaults(handler=batch_password_reset)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
