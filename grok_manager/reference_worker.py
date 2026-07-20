"""Worker executed with the registration runtime embedded in this project.

Browser automation and DrissionPage are loaded only in this subprocess so the
local management server can still start and report missing automation packages.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


OUTPUT_LOCK = threading.Lock()


def emit(prefix: str, payload: Dict[str, Any]) -> None:
    with OUTPUT_LOCK:
        print(prefix + json.dumps(payload, ensure_ascii=False), flush=True)


def is_wrong_password_error(detail: str) -> bool:
    text = str(detail or "").strip()
    return text == "邮箱或密码错误" or text.endswith("邮箱或密码错误")


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


def shutdown_thread_browsers() -> None:
    try:
        from grok_register.cpa_xai.browser_confirm import shutdown_mint_browsers

        shutdown_mint_browsers()
    except Exception:
        pass


def password_reset_core(
    item: Dict[str, Any],
    settings: Dict[str, Any],
    log: Callable[[str], None],
) -> Dict[str, Any]:
    account_id = int(item.get("id") or 0)
    email = str(item.get("email") or "").strip()
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
    if not isinstance(result, dict):
        result = {"ok": False, "error": "密码重置返回无效结果"}
    result["id"] = account_id
    result["email"] = email
    return result


def run_password_reset_item(
    item: Dict[str, Any], settings: Dict[str, Any]
) -> Dict[str, Any]:
    account_id = int(item.get("id") or 0)
    email = str(item.get("email") or "").strip()

    def log(message: str) -> None:
        emit("GM_LOG ", {"id": account_id, "email": email, "message": str(message)})

    log("开始读取重置前邮件")
    result = password_reset_core(item, settings, log)
    emit("GM_RESULT ", result)
    return result


def login_item_core(
    item: Dict[str, Any],
    settings: Dict[str, Any],
    mint_and_export,
    log: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    account_id = int(item.get("id") or 0)
    email = str(item.get("email") or "").strip()
    password = str(item.get("password") or "")
    sso_token = str(item.get("sso_token") or "").strip()
    allow_passwordless = bool(item.get("allow_passwordless"))

    def emit_log(message: str) -> None:
        emit("GM_LOG ", {"id": account_id, "email": email, "message": str(message)})

    log = log or emit_log

    cookies = None
    if sso_token:
        try:
            from grok_register.cpa_xai.browser_confirm import cookies_from_sso

            cookies = cookies_from_sso(sso_token)
            log("injecting stored SSO cookie for device auth (%s clones)" % len(cookies))
        except Exception as exc:
            log("SSO cookie build failed: %s" % exc)
            cookies = None
            if allow_passwordless and not password:
                return {
                    "ok": False,
                    "error": "SSO cookie 构建失败: %s" % exc,
                    "id": account_id,
                    "email": email,
                }

    result = mint_and_export(
        email=email,
        password=password,
        auth_dir=str(item.get("auth_dir") or settings["default_auth_dir"]),
        proxy=str(settings.get("proxy") or "") or None,
        headless=bool(settings.get("headless", False)),
        base_url=str(settings.get("base_url") or "https://cli-chat-proxy.grok.com/v1"),
        probe=bool(settings.get("probe", False)),
        probe_chat=False,
        browser_timeout_sec=float(settings.get("timeout_seconds") or 300),
        force_standalone=True,
        cookies=cookies,
        allow_passwordless=allow_passwordless,
        require_account_gates=bool(settings.get("require_account_gates", True)),
        reuse_browser=bool(settings.get("reuse_browser", True)),
        recycle_every=max(1, int(settings.get("recycle_every") or 10)),
        log=log,
    )
    if not isinstance(result, dict):
        result = {"ok": False, "error": "登录返回无效结果"}
    if result.get("ok"):
        fresh_sso = current_sso_cookie()
        result["sso_token"] = fresh_sso
        result["sso_refreshed"] = bool(fresh_sso)
        if fresh_sso:
            log("fresh sso cookie captured")
        elif allow_passwordless:
            log("CPA reminted via SSO; browser did not expose a new sso cookie")
        else:
            log("OAuth token refreshed but sso cookie was not found")
    result["id"] = account_id
    result["email"] = email
    return result


def recover_wrong_password_item(
    item: Dict[str, Any],
    settings: Dict[str, Any],
    mint_and_export,
) -> Dict[str, Any]:
    """Reset password then re-login once in the same worker thread."""
    account_id = int(item.get("id") or 0)
    email = str(item.get("email") or "").strip()

    def log(message: str) -> None:
        emit("GM_LOG ", {"id": account_id, "email": email, "message": str(message)})

    log("登录密码错误，开始自动重置密码")
    if not str(item.get("mail_credential") or "").strip():
        return {
            "ok": False,
            "id": account_id,
            "email": email,
            "error": "邮箱或密码错误；缺少邮箱访问凭据，无法自动重置密码",
        }

    reset_result = password_reset_core(item, settings, log)
    if not reset_result.get("ok"):
        detail = str(reset_result.get("error") or "自动重置密码失败")
        return {
            "ok": False,
            "id": account_id,
            "email": email,
            "error": "邮箱或密码错误；自动重置密码失败：%s" % detail,
        }

    new_password = str(reset_result.get("password") or "").strip()
    if not new_password:
        return {
            "ok": False,
            "id": account_id,
            "email": email,
            "error": "邮箱或密码错误；自动重置密码未返回新密码",
        }

    log("密码已重置，开始使用新密码重新登录")
    # Call login_item_core directly so recovery cannot chain another auto-reset.
    relogin_item = dict(item)
    relogin_item["password"] = new_password
    result = login_item_core(relogin_item, settings, mint_and_export, log=log)
    result["password"] = new_password
    result["recovered_from_wrong_password"] = True
    if result.get("ok"):
        result["detail"] = "自动重置密码后：批量登录成功"
        result.pop("error", None)
    else:
        retry_detail = str(result.get("error") or result.get("detail") or "重新登录失败")
        result["error"] = "自动重置密码后：%s" % retry_detail
    return result


def run_login_item(
    item: Dict[str, Any],
    settings: Dict[str, Any],
    mint_and_export,
) -> Dict[str, Any]:
    result = login_item_core(item, settings, mint_and_export)
    emit("GM_RESULT ", result)
    return result


def run_login_batch(
    items: List[Dict[str, Any]],
    settings: Dict[str, Any],
    mint_and_export,
    *,
    auto_reset_password: bool = False,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    try:
        for item in items:
            result = login_item_core(item, settings, mint_and_export)
            error = str(result.get("error") or result.get("detail") or "")
            if (
                auto_reset_password
                and not result.get("ok")
                and is_wrong_password_error(error)
                and not bool(item.get("allow_passwordless"))
            ):
                result = recover_wrong_password_item(item, settings, mint_and_export)
            emit("GM_RESULT ", result)
            results.append(result)
        return results
    finally:
        shutdown_thread_browsers()


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

    auto_reset_password = bool(settings.get("auto_reset_password"))
    workers = max(1, min(int(settings.get("workers") or 1), 10, len(accounts)))
    batches = [accounts[index::workers] for index in range(workers)]
    batches = [batch for batch in batches if batch]
    failures = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                run_login_batch,
                batch,
                settings,
                mint_and_export,
                auto_reset_password=auto_reset_password,
            )
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
