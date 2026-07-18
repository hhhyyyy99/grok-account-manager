"""Reset an xAI account password through the account email flow."""

from __future__ import annotations

import secrets
import string
import time
from typing import Any, Callable

from . import app
from .cpa_xai import browser_confirm

LogFn = Callable[[str], None]

SIGN_IN_URL = "https://accounts.x.ai/sign-in"
EMAIL_SELECTOR = "css:input[type='email'], input[name='email'], input[autocomplete='email']"
CODE_SELECTOR = (
    "css:input[name='code'], input[name='verification_code'], "
    "input[autocomplete='one-time-code'], input[data-testid='verification-code']"
)
PASSWORD_SELECTOR = (
    "css:input[name='newPassword'], input[autocomplete='new-password'], "
    "input[name='password'], input[type='password']"
)
SUBMIT_LABELS = [
    "重置密码",
    "Reset password",
    "Update password",
    "更新密码",
    "Save password",
    "保存密码",
    "继续",
    "Continue",
    "完成",
    "Done",
]


class PasswordResetError(RuntimeError):
    pass


def generate_password() -> str:
    alphabet = string.ascii_letters + string.digits
    symbols = "!@#$%^&*"
    chars = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice(symbols),
    ]
    chars.extend(secrets.choice(alphabet + symbols) for _ in range(20))
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def _log_text(page: Any) -> str:
    return browser_confirm._visible_text(page)


def _page_url(page: Any) -> str:
    return browser_confirm._page_url(page)


def _click_links(page: Any, labels: list[str], log: LogFn) -> bool:
    try:
        for selector in ("tag:a", "tag:button"):
            for element in page.eles(selector) or []:
                text = getattr(element, "text", "") or ""
                raw_text = getattr(element, "raw_text", "") or ""
                if callable(raw_text):
                    try:
                        raw_text = raw_text()
                    except Exception:
                        raw_text = ""
                text = browser_confirm._norm(str(text or raw_text or ""))
                if text in labels:
                    element.click(by_js=True)
                    log("已点击 Grok 忘记密码入口")
                    return True
    except Exception:
        pass
    return False


def _fill_code(page: Any, code: str, log: LogFn) -> bool:
    fields = []
    try:
        fields = list(page.eles(CODE_SELECTOR) or [])
    except Exception:
        pass
    compact = "".join(ch for ch in str(code) if ch.isalnum())
    if not compact:
        return False
    if len(fields) > 1:
        try:
            for index, element in enumerate(fields):
                if index >= len(compact):
                    break
                element.clear(by_js=True)
                element.input(compact[index])
            log("filled verification code")
            return True
        except Exception:
            return False


    return browser_confirm._fill(
        page, CODE_SELECTOR, compact, log, "verification code"
    )


def _has_reset_success(url: str, text: str, password_submitted: bool) -> bool:
    low = (text or "").lower()
    success_words = (
        "password reset",
        "password updated",
        "密码已重置",
        "密码已更新",
        "重置成功",
        "reset successfully",
    )
    if any(word in low for word in success_words):
        return True
    return password_submitted and "sign-in" in (url or "").lower() and "new-password" not in low


def _is_new_password_page(url: str, text: str) -> bool:
    low = (text or "").lower()
    path_matches = "reset-password" in (url or "").lower()
    form_markers = (
        "choose a new password",
        "new password",
        "在下方选择一个新密码",
        "新密码",
    )
    action_markers = ("reset password", "update password", "重置密码", "更新密码")
    return path_matches and any(marker in low for marker in form_markers) and any(
        marker in low for marker in action_markers
    )


def _is_unauthorized(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return code == 401 or status == 401 or "401" in str(exc)


def _load_mail_snapshot(
    email: str,
    credential: str,
    log: LogFn,
) -> tuple[set[str], str, bool]:
    log("正在读取重置前邮件")
    try:
        message_ids = app.list_oai_message_ids(credential, email)
    except Exception as exc:
        provider = str(app.get_email_provider() or "").strip().lower()
        if provider != "cloudflare" or not _is_unauthorized(exc):
            raise PasswordResetError("无法读取重置前邮件列表: %s" % exc) from exc
        log("邮箱访问 JWT 已过期，改用管理员邮件接口读取原邮箱")
        try:
            messages = app.cloudflare_admin_get_messages(email)
            message_ids = {
                str(message.get("id") or message.get("message_id"))
                for message in messages
                if message.get("id") or message.get("message_id")
            }
        except Exception as admin_exc:
            raise PasswordResetError(
                "邮箱访问 JWT 已过期，管理员邮件接口读取失败: %s" % admin_exc
            ) from admin_exc
        use_admin_mail = True
    else:
        use_admin_mail = False
    message_ids = {str(value) for value in (message_ids or ())}
    log("重置前邮件快照完成，共 %s 封" % len(message_ids))
    return message_ids, credential, use_admin_mail


def reset_password(
    *,
    email: str,
    mail_credential: str,
    new_password: str = "",
    proxy: str | None = None,
    headless: bool = False,
    timeout_seconds: float = 240.0,
    log: LogFn | None = None,
    cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    log = log or (lambda _message: None)
    email = str(email or "").strip()
    mail_credential = str(mail_credential or "").strip()
    new_password = str(new_password or "").strip() or generate_password()
    if not email or not mail_credential:
        raise PasswordResetError("邮箱或邮箱访问凭据为空")
    if len(new_password) < 12:
        raise PasswordResetError("新密码长度不足")

    excluded_ids, mail_credential, use_admin_mail = _load_mail_snapshot(
        email, mail_credential, log
    )

    browser = page = None
    email_submitted = False
    reset_requested = False
    code_submitted = False
    password_submitted = False
    code = ""
    deadline = time.time() + max(60.0, float(timeout_seconds))
    try:
        browser, page = browser_confirm.create_standalone_page(
            proxy=proxy, headless=headless, log=log
        )
        page.get(SIGN_IN_URL)
        log("已打开 xAI 登录页")
        while time.time() < deadline:
            if cancel and cancel():
                raise PasswordResetError("密码重置已取消")
            url = _page_url(page)
            text = _log_text(page)
            low = text.lower()
            if "too many" in low or "请求过于频繁" in text:
                raise PasswordResetError("xAI 拒绝了过于频繁的重置请求")
            if _has_reset_success(url, text, password_submitted):
                log("Grok 密码重置完成")
                return {"ok": True, "password": new_password}
            if any(label in text for label in ("全部允许", "Accept All Cookies")):
                browser_confirm._click_exact(
                    page, ["全部允许", "Accept All Cookies"], log, real=False
                )
                time.sleep(0.5)

            if not email_submitted and browser_confirm._click_email_login_chooser(
                page, log, text
            ):
                time.sleep(1.0)
                continue

            code_element = page.ele(CODE_SELECTOR, timeout=0.2)
            if code_element:
                if not reset_requested:
                    raise PasswordResetError("未进入 Grok 忘记密码流程却出现验证码页面")
                if not code:
                    log("正在等待 Grok 密码重置验证码邮件")
                    code = app.get_oai_code(
                        mail_credential,
                        email,
                        timeout=app.get_code_poll_timeout(),
                        poll_interval=app.get_code_poll_interval(),
                        log_callback=log,
                        cancel_callback=cancel,
                        cloudflare_admin_address=email if use_admin_mail else "",
                        excluded_message_ids=excluded_ids,
                    )
                if not code_submitted:
                    if not _fill_code(page, code, log):
                        raise PasswordResetError("验证码输入框不可用")
                    if not browser_confirm._click_exact(
                        page, ["继续", "Continue"], log, real=False
                    ):
                        raise PasswordResetError("验证码提交按钮不可用")
                    code_submitted = True
                    log("Grok 密码重置验证码已提交，等待新密码页面")
                    time.sleep(1.2)
                    continue
                if any(
                    marker in low
                    for marker in (
                        "invalid code",
                        "incorrect code",
                        "expired code",
                        "验证码无效",
                        "验证码错误",
                        "验证码已过期",
                    )
                ):
                    raise PasswordResetError("Grok 密码重置验证码无效或已过期")
                time.sleep(0.6)
                continue

            password_elements = []
            try:
                password_elements = list(page.eles(PASSWORD_SELECTOR) or [])
            except Exception:
                pass

            if (
                reset_requested
                and code_submitted
                and password_elements
                and _is_new_password_page(url, text)
            ):
                if password_submitted:
                    time.sleep(0.6)
                    continue
                for element in password_elements[:2]:
                    try:
                        element.clear(by_js=True)
                        element.input(new_password)
                    except Exception as exc:
                        raise PasswordResetError("新密码输入失败") from exc
                log("已填写 Grok 新密码")
                if page.ele("@name=cf-turnstile-response", timeout=0.2):
                    if not browser_confirm._wait_turnstile(page, log, 45):
                        raise PasswordResetError("安全验证未完成")
                if not browser_confirm._click_exact(
                    page, SUBMIT_LABELS, log, real=True
                ):
                    raise PasswordResetError("新密码提交按钮不可用")
                password_submitted = True
                log("已提交 Grok 新密码")
                time.sleep(1.2)
                continue

            if email_submitted and not reset_requested:
                if _click_links(
                    page,
                    [
                        "忘记密码？",
                        "Forgot password?",
                        "Forgot your password?",
                        "Forgot password",
                    ],
                    log,
                ):
                    reset_requested = True
                    log("已进入 Grok 忘记密码流程，等待验证码页面")
                    time.sleep(1.0)
                    continue

            email_element = page.ele(EMAIL_SELECTOR, timeout=0.2)
            if email_element and not email_submitted:
                if not browser_confirm._fill(page, EMAIL_SELECTOR, email, log, "email"):
                    raise PasswordResetError("邮箱输入框不可用")
                if page.ele("@name=cf-turnstile-response", timeout=0.2):
                    if not browser_confirm._wait_turnstile(page, log, 45):
                        raise PasswordResetError("安全验证未完成")
                if browser_confirm._click_exact(
                    page, ["下一步", "Next", "继续", "Continue"], log, real=False
                ):
                    email_submitted = True
                    log("已提交 Grok 登录邮箱，等待密码页面")
                    time.sleep(1.0)
                    continue

            time.sleep(0.6)
        raise PasswordResetError("密码重置页面在规定时间内未完成")
    except PasswordResetError:
        raise
    except Exception as exc:
        raise PasswordResetError("密码重置浏览器流程失败: %s" % exc) from exc
    finally:
        if browser is not None:
            try:
                browser_confirm.close_standalone(browser)
            except Exception:
                pass
