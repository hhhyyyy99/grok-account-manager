import json
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grok_manager.models import AccountDraft, AccountStatus
from tests.support import make_manager
from grok_register import app as registration_app
from grok_register.password_reset import (
    CODE_SELECTOR,
    EMAIL_SELECTOR,
    PASSWORD_SELECTOR,
    SIGN_IN_URL,
    _has_reset_success,
    _load_mail_snapshot,
    generate_password,
    reset_password,
)


class _ResetFlowElement:
    def __init__(self, page, kind, text=""):
        self.page = page
        self.kind = kind
        self.text = text
        self.value = ""

    def clear(self, by_js=False):
        self.value = ""

    def input(self, value):
        self.value = str(value)

    def click(self, by_js=False):
        transitions = {
            "chooser": "email",
            "email-submit": "password",
            "forgot": "code",
            "code-submit": "new-password",
            "password-submit": "success",
        }
        if self.kind in transitions:
            self.page.stage = transitions[self.kind]


class _ResetFlowPage:
    def __init__(self):
        self.opened_url = ""
        self.stage = "chooser"
        self.email = _ResetFlowElement(self, "email")
        self.code = _ResetFlowElement(self, "code")
        self.password = _ResetFlowElement(self, "password")
        self.password_confirm = _ResetFlowElement(self, "password-confirm")

    @property
    def url(self):
        if self.stage in {"chooser", "email", "password"}:
            return "https://accounts.x.ai/sign-in?email=true"
        if self.stage == "success":
            return "https://accounts.x.ai/sign-in"
        return "https://accounts.x.ai/reset-password"

    def get(self, url):
        self.opened_url = url
        return None

    def run_js(self, _script):
        return {
            "chooser": "使用邮箱登录",
            "email": "使用您的邮箱登录 邮箱 下一步",
            "password": "Accept All Cookies Email Password Forgot your password? Sign in",
            "code": "验证您的邮箱 6 位验证码 继续",
            "new-password": "设置新密码 确认密码 重置密码",
            "success": "密码已更新",
        }[self.stage]

    def ele(self, selector, timeout=0.0):
        if selector == CODE_SELECTOR:
            return self.code if self.stage == "code" else None
        if selector == PASSWORD_SELECTOR:
            return self.password if self.stage in {"password", "new-password"} else None
        if selector == EMAIL_SELECTOR:
            return self.email if self.stage == "email" else None
        return None


    def eles(self, selector):
        if selector == "tag:button":
            if self.stage == "password":
                return [_ResetFlowElement(self, "cookie", "Accept All Cookies")]
            buttons = {
                "chooser": ("chooser", "使用邮箱登录"),
                "email": ("email-submit", "下一步"),
                "code": ("code-submit", "继续"),
                "new-password": ("password-submit", "重置密码"),
            }
            return [_ResetFlowElement(self, *buttons[self.stage])] if self.stage in buttons else []
        if selector == "tag:a":
            if self.stage == "password":
                return [_ResetFlowElement(self, "forgot", "Forgot your password?")]
            return []
        if selector == CODE_SELECTOR:
            return [self.code] if self.stage == "code" else []
        if selector == PASSWORD_SELECTOR:
            if self.stage == "new-password":
                return [self.password, self.password_confirm]
        return []


class PasswordResetTests(unittest.TestCase):
    def test_reset_password_follows_sign_in_email_forgot_password_flow(self) -> None:
        page = _ResetFlowPage()
        clock = iter(range(1000))
        logs = []
        with patch(
            "grok_register.password_reset._load_mail_snapshot",
            return_value=(set(), "mail-token", False),
        ):
            with patch(
                "grok_register.password_reset.app.get_oai_code", return_value="ABC-DEF"
            ):
                with patch(
                    "grok_register.password_reset.browser_confirm.create_standalone_page",
                    return_value=(object(), page),
                ):
                    with patch(
                        "grok_register.password_reset.browser_confirm.close_standalone"
                    ):
                        with patch(
                            "grok_register.password_reset.time.time",
                            side_effect=lambda: next(clock),
                        ):
                            with patch("grok_register.password_reset.time.sleep"):
                                result = reset_password(
                                    email="target@example.com",
                                    mail_credential="mail-token",
                                    new_password="New-password-123!",
                                    timeout_seconds=60,
                                    log=logs.append,
                                )

        self.assertEqual(True, result["ok"])
        self.assertEqual("New-password-123!", result["password"])
        self.assertEqual("success", page.stage)
        self.assertEqual(SIGN_IN_URL, page.opened_url)
        self.assertEqual("ABCDEF", page.code.value)

    def test_reference_finds_source_mail_credential_and_persists_password(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            batch = manager.reference.output_dir / "batch"
            batch.mkdir(parents=True)
            source = batch / "accounts.txt"
            source.write_text(
                "target@example.com----old-password----old-sso\n",
                encoding="utf-8",
            )
            (batch / "mail_credentials.txt").write_text(
                "target@example.com\tmail-token\n",
                encoding="utf-8",
            )

            self.assertEqual(
                "mail-token",
                manager.reference.find_mail_credential(
                    "TARGET@example.com", str(source)
                ),
            )
            path = manager.reference.persist_account_password(
                "target@example.com", "New-password-123!", str(source)
            )

            self.assertEqual(manager.vault.path, path)
            self.assertEqual(
                "target@example.com----old-password----old-sso\n",
                source.read_text(encoding="utf-8"),
            )

    def test_password_reset_result_updates_database_without_exposing_password(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            source = manager.reference.output_dir / "batch" / "accounts.txt"
            source.parent.mkdir(parents=True)
            source.write_text(
                "target@example.com----old-password----old-sso\n",
                encoding="utf-8",
            )
            account = manager.store.upsert(
                AccountDraft(
                    email="target@example.com",
                    password="old-password",
                    sso_token="old-sso",
                    source=str(source),
                )
            )

            result = manager.password_reset._handle_result(
                json.dumps(
                    {
                        "id": account.id,
                        "email": account.email,
                        "ok": True,
                        "password": "New-password-123!",
                    }
                )
            )
            stored = manager.store.get(account.id)

            self.assertEqual((True, "密码已重置，已写入 credentials.vault.json"), (result.ok, result.detail))
            self.assertEqual(
                ("New-password-123!", AccountStatus.NEEDS_LOGIN.value),
                (stored.password if stored else "", stored.status if stored else ""),
            )
            self.assertNotIn("New-password-123!", result.detail)

    def test_reset_code_polling_passes_old_message_exclusions(self) -> None:
        with patch.object(registration_app, "get_email_provider", return_value="cloudflare"):
            with patch.object(
                registration_app,
                "cloudflare_get_oai_code",
                return_value="ABC-DEF",
            ) as get_code:
                result = registration_app.get_oai_code(
                    "mail-token",
                    "target@example.com",
                    excluded_message_ids={"old-message"},
                )

        self.assertEqual("ABC-DEF", result)
        self.assertEqual({"old-message"}, get_code.call_args.kwargs["excluded_message_ids"])

    def test_reset_worker_emits_boundary_log_before_mail_snapshot(self) -> None:
        from grok_manager import reference_worker

        events = []

        def capture(prefix, payload):
            events.append((prefix, payload))

        with patch("grok_manager.reference_worker.emit", side_effect=capture):
            with patch(
                "grok_register.password_reset.reset_password",
                return_value={"ok": False, "error": "test"},
            ):
                reference_worker.run_password_reset_item(
                    {
                        "id": 7,
                        "email": "target@example.com",
                        "mail_credential": "mail-token",
                    },
                    {"timeout_seconds": 60},
                )

        messages = [payload["message"] for prefix, payload in events if prefix == "GM_LOG "]
        self.assertIn("开始读取重置前邮件", messages[0])
    def test_unauthorized_mail_snapshot_renews_jwt_via_address_id(self) -> None:
        header = base64.urlsafe_b64encode(b"{}").decode().rstrip("=")
        claims = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "address": "target@example.com",
                    "address_id": 4640,
                }
            ).encode()
        ).decode().rstrip("=")
        expired = "%s.%s.sig" % (header, claims)
        calls = []

        def list_ids(credential, _email):
            calls.append(credential)
            if len(calls) == 1:
                raise RuntimeError("HTTP Error 401:")
            return {"new-message"}

        logs = []
        with patch.object(registration_app, "get_email_provider", return_value="cloudflare"):
            with patch.object(registration_app, "list_oai_message_ids", side_effect=list_ids):
                with patch.object(
                    registration_app,
                    "cloudflare_admin_get_jwt",
                    return_value="fresh-jwt",
                    create=True,
                ) as get_jwt:
                    ids, credential, use_admin = _load_mail_snapshot(
                        "target@example.com", expired, logs.append
                    )

        self.assertEqual(({"new-message"}, "fresh-jwt", False), (ids, credential, use_admin))
        self.assertEqual([expired, "fresh-jwt"], calls)
        get_jwt.assert_called_once_with("4640")
        self.assertTrue(any("JWT" in message and "续期" in message for message in logs))

    def test_unauthorized_mail_snapshot_uses_cloudflare_admin_fallback(self) -> None:
        logs = []
        with patch.object(registration_app, "get_email_provider", return_value="cloudflare"):
            with patch.object(
                registration_app,
                "list_oai_message_ids",
                side_effect=RuntimeError("HTTP Error 401:"),
            ):
                with patch.object(
                    registration_app,
                    "cloudflare_admin_get_messages",
                    return_value=[{"id": "old-message"}],
                    create=True,
                ) as admin_messages:
                    ids, credential, use_admin = _load_mail_snapshot(
                        "target@example.com", "old-token", logs.append
                    )
        self.assertEqual(({"old-message"}, "old-token", True), (ids, credential, use_admin))
        admin_messages.assert_called_once_with("target@example.com")
        self.assertTrue(any("管理员邮件接口" in message for message in logs))

    def test_cloudflare_admin_messages_are_scoped_to_original_address(self) -> None:
        class Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "results": [
                        {
                            "id": 9,
                            "address": "target@example.com",
                            "raw": "verification code ABC-DEF",
                        }
                    ],
                    "count": 1,
                }

        with patch.dict(
            registration_app.config,
            {
                "cloudflare_api_base": "https://mail.test",
                "cloudflare_auth_mode": "x-admin-auth",
                "cloudflare_api_key": "secret",
            },
            clear=False,
        ):
            with patch.object(
                registration_app,
                "http_get",
                return_value=Response(),
            ) as get:
                messages = registration_app.cloudflare_admin_get_messages(
                    "TARGET@example.com"
                )

        self.assertEqual(9, messages[0]["id"])
        self.assertEqual(
            "target@example.com",
            get.call_args.kwargs["params"]["address"],
        )


    def test_cloudflare_admin_get_jwt_reads_show_password_response(self) -> None:
        class Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"jwt": "fresh-jwt"}

        with patch.dict(
            registration_app.config,
            {
                "cloudflare_api_base": "https://mail.test",
                "cloudflare_auth_mode": "x-admin-auth",
                "cloudflare_api_key": "secret",
            },
            clear=False,
        ):
            with patch.object(registration_app, "http_get", return_value=Response()) as get:
                token = registration_app.cloudflare_admin_get_jwt("4640")

        self.assertEqual("fresh-jwt", token)
        self.assertTrue(get.call_args.args[0].endswith("/admin/show_password/4640"))
        self.assertEqual("secret", get.call_args.kwargs["headers"]["x-admin-auth"])

    def test_generated_password_has_required_character_classes(self) -> None:
        password = generate_password()
        self.assertGreaterEqual(len(password), 24)
        self.assertRegex(password, r"[A-Z]")
        self.assertRegex(password, r"[a-z]")
        self.assertRegex(password, r"[0-9]")
        self.assertRegex(password, r"[!@#$%^&*]")

    def test_reset_success_detection_accepts_xai_confirmation(self) -> None:
        self.assertTrue(
            _has_reset_success(
                "https://accounts.x.ai/sign-in", "Password updated", True
            )
        )
        self.assertTrue(
            _has_reset_success(
                "https://accounts.x.ai/sign-in",
                "You can now sign in with your new password",
                True,
            )
        )
        self.assertTrue(
            _has_reset_success(
                "https://accounts.x.ai/sign-in?email=true",
                "使用邮箱登录",
                True,
                password_form_present=False,
            )
        )
        self.assertTrue(
            _has_reset_success(
                "https://accounts.x.ai/account",
                "",
                True,
                password_form_present=False,
            )
        )
        self.assertFalse(
            _has_reset_success("https://accounts.x.ai/sign-in", "Sign in", False)
        )
        self.assertFalse(
            _has_reset_success(
                "https://accounts.x.ai/reset-password",
                "设置新密码 确认密码 重置密码",
                True,
                password_form_present=True,
            )
        )


if __name__ == "__main__":
    unittest.main()
