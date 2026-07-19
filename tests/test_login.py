import json
import os
import sys
import tempfile
import textwrap
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import DrissionPage
from grok_manager.login import LoginSettings
from grok_manager.models import AccountDraft, AccountStatus, LoginResult
from grok_manager.paths import MANAGED_AUTH_DIR
from grok_register.cpa_xai import browser_confirm, oauth_device
from grok_register.paths import TURNSTILE_DIR
from tests.support import make_manager


def install_fake_login_modules(
    reference_root: Path,
    auth_email: str,
    sso_token: str = "fresh-sso",
) -> None:
    package = reference_root / "grok_register" / "cpa_xai"
    package.mkdir(parents=True)
    (reference_root / "grok_register" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "browser_confirm.py").write_text(
        textwrap.dedent(
            """
            import threading

            _local = threading.local()

            class Page:
                def __init__(self, token):
                    self.token = token

                def cookies(self, all_domains=True, all_info=True):
                    return [{"name": "sso", "value": self.token}]

            def _mint_tls_get():
                state = getattr(_local, "state", None)
                if state is None:
                    state = {"page": None}
                    _local.state = state
                return state

            def set_sso(token):
                _mint_tls_get()["page"] = Page(token)

            def shutdown_mint_browsers():
                _mint_tls_get()["page"] = None
            """
        ),
        encoding="utf-8",
    )
    mint_source = textwrap.dedent(
        """
        import json
        from pathlib import Path

        from .browser_confirm import set_sso

        AUTH_EMAIL = __AUTH_EMAIL__

        def mint_and_export(**values):
            email = values["email"]
            auth_dir = Path(values["auth_dir"])
            auth_dir.mkdir(parents=True, exist_ok=True)
            path = auth_dir / ("xai-%s.json" % email)
            path.write_text(
                json.dumps(
                    {
                        "email": AUTH_EMAIL,
                        "access_token": "fresh-access",
                        "refresh_token": "fresh-refresh",
                        "expired": "2099-01-01T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            set_sso(__SSO_TOKEN__)
            return {"ok": True, "path": str(path)}
        """
    ).replace("__AUTH_EMAIL__", repr(auth_email)).replace("__SSO_TOKEN__", repr(sso_token))
    (package / "mint.py").write_text(
        mint_source,
        encoding="utf-8",
    )


class BatchLoginCredentialTests(unittest.TestCase):
    def test_email_login_chooser_prefers_stable_test_id(self) -> None:
        clicks = []

        class FakeElement:
            def click(self, *, by_js=False):
                clicks.append(by_js)

        class FakePage:
            def ele(self, selector, timeout=0):
                self.request = (selector, timeout)
                return FakeElement()

        page = FakePage()
        logs = []

        clicked = browser_confirm._click_email_login_chooser(page, logs.append)

        self.assertTrue(clicked)
        self.assertEqual(
            ("css:button[data-testid='continue-with-email']", 0.3),
            page.request,
        )
        self.assertEqual([True], clicks)

    def test_fill_clears_field_without_logging_credential(self) -> None:
        actions = []

        class FakeElement:
            def __init__(self):
                self.value = ""

            def clear(self, by_js=False):
                actions.append(("clear", by_js))
                if by_js:
                    self.value = ""

            def input(self, value):
                actions.append(("input", value))
                self.value += value

        element = FakeElement()

        class FakePage:
            def ele(self, selector, timeout=0):
                self.request = (selector, timeout)
                return element

        page = FakePage()
        logs = []

        filled = browser_confirm._fill(
            page, "css:input[type='password']", "secret-value", logs.append, "password"
        )

        self.assertTrue(filled)
        self.assertEqual(("css:input[type='password']", 0.8), page.request)
        self.assertEqual([("clear", True), ("input", "secret-value")], actions)
        self.assertNotIn("secret-value", " ".join(logs))

    def test_fill_replaces_password_when_physical_clear_does_not_work(self) -> None:
        class FakeElement:
            def __init__(self):
                self.value = ""

            def clear(self, by_js=False):
                if by_js:
                    self.value = ""

            def input(self, value):
                self.value += value

        element = FakeElement()

        class FakePage:
            def ele(self, _selector, timeout=0):
                return element

        page = FakePage()
        for _ in range(2):
            self.assertTrue(
                browser_confirm._fill(
                    page,
                    "css:input[type='password']",
                    "123456",
                    lambda _message: None,
                    "password",
                )
            )

        self.assertEqual("123456", element.value)

    def test_password_is_filled_before_waiting_for_turnstile(self) -> None:
        events = []
        selectors = {}

        def fake_fill(_page, _selector, _value, _log, field_name):
            events.append(("fill", field_name))
            selectors[field_name] = _selector
            return True

        def fake_wait(_page, _log, timeout):
            events.append(("turnstile", timeout))
            return True

        with patch.object(browser_confirm, "_fill", side_effect=fake_fill), patch.object(
            browser_confirm, "_wait_turnstile", side_effect=fake_wait
        ):
            ready = browser_confirm._prepare_password_login(
                object(), "email", "password", lambda _: None
            )

        self.assertTrue(ready)
        self.assertEqual(
            [
                ("fill", "email"),
                ("fill", "password"),
                ("turnstile", 45),
            ],
            events,
        )
        self.assertIn("input[name='password']", selectors["password"])

    def test_password_step_preserves_existing_readonly_email(self) -> None:
        class FakeElement:
            def __init__(self, value="", readonly=False):
                self.value = value
                self.readonly = readonly

            def clear(self, by_js=False):
                if by_js:
                    self.value = ""

            def input(self, value):
                if not self.readonly:
                    self.value += value

        email = "target@example.com"
        email_element = FakeElement(email, readonly=True)
        password_element = FakeElement()

        class FakePage:
            def ele(self, selector, timeout=0):
                if "type='email'" in selector:
                    return email_element
                return password_element

        with patch.object(browser_confirm, "_wait_turnstile", return_value=True):
            ready = browser_confirm._prepare_password_login(
                FakePage(), email, "123456", lambda _message: None
            )

        self.assertTrue(ready)
        self.assertEqual(email, email_element.value)
        self.assertEqual("123456", password_element.value)

    def test_login_rejects_invalid_credentials_message(self) -> None:
        samples = (
            "Wrong email address or password.",
            "The email or password you entered is incorrect.",
            "Incorrect password",
            "邮箱地址或密码错误",
            "错误的邮箱地址或密码",
            "错误的邮箱或密码",
            "密码不正确",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                with self.assertRaisesRegex(
                    browser_confirm.BrowserConfirmError, "邮箱或密码错误"
                ):
                    browser_confirm._raise_for_login_error(sample)

        # Co-occurrence of password + invalid must not invent a credential error.
        browser_confirm._raise_for_login_error("Password\nInvalid request")
        browser_confirm._raise_for_login_error("Invalid action")

    def test_password_page_stuck_does_not_invent_wrong_password_error(self) -> None:
        class FakeElement:
            def clear(self):
                return None

            def input(self, _value):
                return None

            def click(self, by_js=False):
                return None

        class FakePage:
            def ele(self, selector, timeout=0):
                text = str(selector)
                if "user_code" in text or "continue-with-email" in text:
                    return None
                if "type='email'" in text or "type=\"email\"" in text:
                    return FakeElement()
                if "password" in text:
                    return FakeElement()
                if "submit" in text or "sign-in-submit" in text:
                    return FakeElement()
                return None

            def eles(self, _selector):
                return []

            def get(self, _url, timeout=None):
                return None

            def run_js(self, script):
                if "innerText" in str(script):
                    # Stuck password page without an explicit credential error.
                    return "Sign in"
                return ""

        page = FakePage()
        logs = []

        with patch.object(browser_confirm, "_wait_turnstile", return_value=True), patch.object(
            browser_confirm, "_click_exact", return_value=True
        ), patch.object(browser_confirm, "_sleep", return_value=None), patch.object(
            browser_confirm, "_page_url", return_value="https://accounts.x.ai/sign-in"
        ), patch.object(browser_confirm, "_click_email_login_chooser", return_value=False):
            with self.assertRaisesRegex(
                browser_confirm.BrowserConfirmError,
                r"未检测到明确的邮箱或密码错误|浏览器登录未完成",
            ):
                browser_confirm.approve_device_code(
                    page,
                    verification_uri_complete="https://accounts.x.ai/oauth2/device?user_code=ABCD",
                    email="target@example.com",
                    password="wrong-password",
                    user_code="ABCD",
                    timeout_sec=30,
                    log=logs.append,
                )

        self.assertTrue(any("login attempt" in line for line in logs))

    def test_password_page_surfaces_explicit_wrong_password_error(self) -> None:
        class FakeElement:
            def clear(self):
                return None

            def input(self, _value):
                return None

            def click(self, by_js=False):
                return None

        class FakePage:
            def ele(self, selector, timeout=0):
                text = str(selector)
                if "user_code" in text or "continue-with-email" in text:
                    return None
                if "type='email'" in text or "type=\"email\"" in text:
                    return FakeElement()
                if "password" in text:
                    return FakeElement()
                if "submit" in text or "sign-in-submit" in text:
                    return FakeElement()
                return None

            def eles(self, _selector):
                return []

            def get(self, _url, timeout=None):
                return None

            def run_js(self, script):
                if "innerText" in str(script):
                    return "Wrong email address or password."
                return ""

        with patch.object(browser_confirm, "_wait_turnstile", return_value=True), patch.object(
            browser_confirm, "_click_exact", return_value=True
        ), patch.object(browser_confirm, "_sleep", return_value=None), patch.object(
            browser_confirm, "_page_url", return_value="https://accounts.x.ai/sign-in"
        ), patch.object(browser_confirm, "_click_email_login_chooser", return_value=False):
            with self.assertRaisesRegex(browser_confirm.BrowserConfirmError, r"^邮箱或密码错误$"):
                browser_confirm.approve_device_code(
                    FakePage(),
                    verification_uri_complete="https://accounts.x.ai/oauth2/device?user_code=ABCD",
                    email="target@example.com",
                    password="wrong-password",
                    user_code="ABCD",
                    timeout_sec=30,
                    log=lambda _message: None,
                )

    def test_oauth_poll_retries_transient_network_error(self) -> None:
        response = {
            "access_token": "access",
            "refresh_token": "refresh",
            "token_type": "Bearer",
            "expires_in": 3600,
        }
        logs = []

        with patch.object(
            oauth_device,
            "_post_form",
            side_effect=[urllib.error.URLError("transient"), (200, response)],
        ), patch.object(oauth_device.time, "sleep"):
            result = oauth_device.poll_device_token(
                "device-code", expires_in=60, log=logs.append
            )

        self.assertEqual("access", result.access_token)
        self.assertTrue(any("network error" in line for line in logs))

    def test_oauth_refresh_access_token_keeps_or_rotates_refresh(self) -> None:
        rotated = {
            "access_token": "new-access",
            "refresh_token": "rotated-refresh",
            "token_type": "Bearer",
            "expires_in": 7200,
        }
        reused = {
            "access_token": "new-access-2",
            "token_type": "Bearer",
            "expires_in": 3600,
        }
        with patch.object(oauth_device, "_post_form", return_value=(200, rotated)):
            first = oauth_device.refresh_access_token("old-refresh")
        with patch.object(oauth_device, "_post_form", return_value=(200, reused)):
            second = oauth_device.refresh_access_token("old-refresh")

        self.assertEqual(
            ("new-access", "rotated-refresh", "new-access-2", "old-refresh"),
            (
                first.access_token,
                first.refresh_token,
                second.access_token,
                second.refresh_token,
            ),
        )

    def test_oauth_refresh_access_token_surfaces_invalid_grant(self) -> None:
        with patch.object(
            oauth_device,
            "_post_form",
            return_value=(400, {"error": "invalid_grant", "error_description": "expired"}),
        ):
            with self.assertRaises(oauth_device.OAuthDeviceError) as raised:
                oauth_device.refresh_access_token("dead-refresh")
        self.assertIn("invalid_grant", str(raised.exception))

    def test_batch_refresh_cpa_updates_tokens_without_touching_sso(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            hotload_dir = root / "cpa-hotload"
            manager.reference.config_file.write_text(
                json.dumps(
                    {
                        "cpa_copy_to_hotload": True,
                        "cpa_hotload_dir": str(hotload_dir),
                        "cpa_base_url": "https://cli-chat-proxy.grok.com/v1",
                    }
                ),
                encoding="utf-8",
            )
            account = manager.store.upsert(
                AccountDraft(
                    email="refresh@example.com",
                    password="password",
                    sso_token="keep-sso",
                    access_token="old-access",
                    refresh_token="old-refresh",
                    token_expires_at="2020-01-01T00:00:00Z",
                )
            )
            manager.store.set_status(
                [account.id], AccountStatus.EXPIRED.value, "CPA access token 已过期"
            )
            token = oauth_device.TokenResult(
                access_token="fresh-access",
                refresh_token="fresh-refresh",
                id_token=None,
                token_type="Bearer",
                expires_in=21600,
                raw={},
            )

            with patch.object(
                oauth_device,
                "refresh_access_token",
                return_value=token,
            ), patch.object(
                manager,
                "inspect_accounts",
                return_value=[],
            ):
                result = manager.batch_refresh_cpa([account.id])[0]
            stored = manager.store.get(account.id)
            auth_files = list(manager.reference.managed_auth_dir.glob("xai-*.json"))

            self.assertTrue(result.ok)
            self.assertEqual("keep-sso", stored.sso_token if stored else "")
            self.assertEqual("fresh-access", stored.access_token if stored else "")
            self.assertEqual("fresh-refresh", stored.refresh_token if stored else "")
            # Managed auth files are transient and must be cleaned after vault write.
            self.assertEqual([], auth_files)
            hotload_files = list(hotload_dir.glob("xai-*.json"))
            self.assertEqual(1, len(hotload_files))
            payload = json.loads(hotload_files[0].read_text(encoding="utf-8"))
            self.assertEqual("fresh-access", payload["access_token"])
            self.assertEqual("fresh-refresh", payload["refresh_token"])

    def test_batch_refresh_cpa_requires_refresh_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(
                    email="no-refresh@example.com",
                    password="password",
                    access_token="old-access",
                )
            )
            result = manager.batch_refresh_cpa([account.id])[0]
            self.assertFalse(result.ok)
            self.assertIn("缺少 refresh_token", result.detail)
            self.assertIn("批量登录", result.detail)

    def test_batch_refresh_cpa_falls_back_to_sso_remint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            account = manager.store.upsert(
                AccountDraft(
                    email="sso-remint@example.com",
                    password="password",
                    sso_token="live-sso",
                    access_token="old-access",
                    refresh_token="revoked-refresh",
                    token_expires_at="2020-01-01T00:00:00Z",
                )
            )
            auth_path = manager.reference.managed_auth_dir / "xai-sso-remint@example.com.json"
            auth_path.parent.mkdir(parents=True, exist_ok=True)
            auth_path.write_text(
                json.dumps(
                    {
                        "email": "sso-remint@example.com",
                        "access_token": "reminted-access",
                        "refresh_token": "reminted-refresh",
                        "expired": "2099-01-01T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )

            def fake_remint(account_ids, settings, log=None, progress=None):
                del settings
                from grok_manager.models import CpaRefreshResult

                ids = list(account_ids)
                results = []
                for index, account_id in enumerate(ids, start=1):
                    stored = manager.store.get(account_id)
                    manager.store.apply_cpa_credentials(
                        account_id,
                        "reminted-access",
                        "reminted-refresh",
                        "2099-01-01T00:00:00Z",
                        str(auth_path),
                        detail="通过 SSO 重新签发 CPA 凭据",
                    )
                    remint_result = CpaRefreshResult(
                        account_id,
                        stored.email if stored else "",
                        True,
                        "通过 SSO 重新签发 CPA 凭据",
                        auth_file=str(auth_path),
                    )
                    results.append(remint_result)
                    if progress:
                        progress(remint_result, index, len(ids))
                    if log:
                        log("[%s] reminted" % (stored.email if stored else account_id))
                return results

            with patch.object(
                oauth_device,
                "refresh_access_token",
                side_effect=oauth_device.OAuthDeviceError(
                    "refresh token failed HTTP 400: invalid_grant: Refresh token has been revoked"
                ),
            ), patch.object(
                manager.login,
                "remint_cpa_via_sso",
                side_effect=fake_remint,
            ), patch.object(
                manager,
                "inspect_accounts",
                return_value=[],
            ):
                result = manager.batch_refresh_cpa([account.id])[0]
            stored = manager.store.get(account.id)

            self.assertTrue(result.ok)
            self.assertIn("SSO", result.detail)
            self.assertEqual("live-sso", stored.sso_token if stored else "")
            self.assertEqual("reminted-access", stored.access_token if stored else "")
            self.assertEqual("reminted-refresh", stored.refresh_token if stored else "")

    def test_mint_and_export_allows_passwordless_with_cookies(self) -> None:
        from grok_register.cpa_xai import mint as mint_module

        with tempfile.TemporaryDirectory() as directory:
            auth_dir = Path(directory)
            with patch.object(
                mint_module,
                "mint_with_browser",
                return_value={
                    "access_token": "cookie-access",
                    "refresh_token": "cookie-refresh",
                    "expires_in": 3600,
                },
            ) as mint_browser:
                result = mint_module.mint_and_export(
                    email="cookie@example.com",
                    password="",
                    auth_dir=auth_dir,
                    cookies=[{"name": "sso", "value": "live"}],
                    allow_passwordless=True,
                    probe=False,
                )
            self.assertTrue(result["ok"])
            self.assertTrue(mint_browser.call_args.kwargs["allow_passwordless"])
            missing = mint_module.mint_and_export(
                email="cookie@example.com",
                password="",
                auth_dir=auth_dir,
                allow_passwordless=True,
                probe=False,
            )
            self.assertFalse(missing["ok"])
            self.assertIn("password", missing["error"])

    def test_cookies_from_sso_expands_domains(self) -> None:
        cookies = browser_confirm.cookies_from_sso("abc123")
        names = {(item["name"], item["domain"]) for item in cookies}
        self.assertIn(("sso", "accounts.x.ai"), names)
        self.assertIn(("sso-rw", ".x.ai"), names)
        self.assertEqual("abc123", cookies[0]["value"])

    def test_login_fallback_uses_packaged_turnstile_extension(self) -> None:
        extensions = []

        class FakeOptions:
            def set_timeouts(self, **_values):
                return None

            def set_argument(self, _value):
                return None

            def add_extension(self, value):
                extensions.append(value)

            def headless(self, _value):
                return None

            def auto_port(self):
                return None

        fake_app = types.ModuleType("grok_register.app")
        fake_app.create_browser_options = lambda **_values: None
        with patch.dict(sys.modules, {"grok_register.app": fake_app}):
            with patch.object(DrissionPage, "ChromiumOptions", FakeOptions):
                browser_confirm._build_mint_browser_options()

        self.assertEqual([str(TURNSTILE_DIR)], extensions)

    def test_login_rejects_auth_file_for_another_email(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            install_fake_login_modules(manager.reference.root, "other@example.com")
            account = manager.store.upsert(
                AccountDraft(
                    email="target@example.com",
                    password="password",
                    sso_token="old-sso",
                    access_token="old-access",
                    refresh_token="old-refresh",
                    auth_file=str(root / "auth" / "xai-target@example.com.json"),
                )
            )

            result = manager.batch_login([account.id])[0]
            stored = manager.store.get(account.id)

            self.assertEqual(
                (False, "old-access"),
                (result.ok, stored.access_token if stored else ""),
            )

    def test_login_keeps_account_status_until_result_arrives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(email="expired@example.com", password="password")
            )
            manager.store.set_status(
                [account.id], AccountStatus.EXPIRED.value, "原有凭据已过期"
            )
            observed_statuses = []

            class ObservingOutput:
                def __iter__(self):
                    stored = manager.store.get(account.id)
                    observed_statuses.append(stored.status if stored else "")
                    return iter(())

                def close(self):
                    return None

            class FakeProcess:
                stdout = ObservingOutput()

                @staticmethod
                def wait():
                    return 0

            with patch(
                "grok_manager.worker_runtime.subprocess.Popen", return_value=FakeProcess()
            ):
                manager.login.login_accounts([account.id], LoginSettings())

            self.assertEqual([AccountStatus.EXPIRED.value], observed_statuses)

    def test_auto_import_keeps_managed_login_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            output = manager.reference.output_dir / "out_old"
            output.mkdir(parents=True)
            accounts_file = output / "accounts.txt"
            accounts_file.write_text(
                "persisted@example.com----password----old-sso\n",
                encoding="utf-8",
            )
            old_auth = output / "xai-persisted@example.com.json"
            old_auth.write_text(
                json.dumps(
                    {
                        "email": "persisted@example.com",
                        "access_token": "old-access",
                        "refresh_token": "old-refresh",
                    }
                ),
                encoding="utf-8",
            )
            os.utime(accounts_file, (1000, 1000))
            os.utime(old_auth, (1000, 1000))
            account = manager.import_reference_accounts()[0]

            managed_auth = manager.reference.managed_auth_dir / old_auth.name
            managed_auth.parent.mkdir(parents=True, exist_ok=True)
            managed_auth.write_text(
                json.dumps(
                    {
                        "email": "persisted@example.com",
                        "access_token": "fresh-access",
                        "refresh_token": "fresh-refresh",
                    }
                ),
                encoding="utf-8",
            )
            os.utime(managed_auth, (2000, 2000))
            manager.store.apply_login_credentials(
                account.id,
                "fresh-access",
                "fresh-refresh",
                "2099-01-01T00:00:00Z",
                str(managed_auth),
                sso_token="fresh-sso",
            )

            indexed_path, _payload = manager.reference.build_auth_index()[
                "persisted@example.com"
            ]
            manager.import_reference_accounts()
            stored = manager.store.get(account.id)

            self.assertEqual(
                (
                    managed_auth,
                    "fresh-sso",
                    "fresh-access",
                    "fresh-refresh",
                    managed_auth,
                ),
                (
                    indexed_path,
                    stored.sso_token if stored else "",
                    stored.access_token if stored else "",
                    stored.refresh_token if stored else "",
                    Path(stored.auth_file) if stored else Path(),
                ),
            )

    def test_login_syncs_cpa_hotload_and_logs_review_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            hotload_dir = root / "cpa-hotload"
            manager.reference.config_file.write_text(
                json.dumps(
                    {
                        "cpa_copy_to_hotload": True,
                        "cpa_hotload_dir": str(hotload_dir),
                    }
                ),
                encoding="utf-8",
            )
            future_sso = "e30.eyJleHAiOjQxMDI0NDQ4MDB9.sig"
            install_fake_login_modules(
                manager.reference.root,
                "hotload@example.com",
                sso_token=future_sso,
            )
            account = manager.store.upsert(
                AccountDraft(
                    email="hotload@example.com",
                    password="password",
                )
            )
            logs = []

            result = manager.batch_login([account.id], log=logs.append)[0]
            stored = manager.store.get(account.id)
            hotloaded = json.loads(
                (hotload_dir / "xai-hotload@example.com.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(
                (True, "active", "active", "active", "fresh-access"),
                (
                    result.ok,
                    stored.status if stored else "",
                    stored.sso_status if stored else "",
                    stored.cpa_status if stored else "",
                    hotloaded.get("access_token"),
                ),
            )
            self.assertTrue(any("CPA hotload 已更新" in line for line in logs))
            self.assertTrue(any("复核完成: SSO=正常，CPA=正常" in line for line in logs))

    def test_relogin_passes_previous_sso_to_grok2api_sync(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(
                    email="first@example.com",
                    password="password",
                    sso_token="fresh-sso",
                    access_token="access",
                    refresh_token="refresh",
                )
            )
            result = LoginResult(
                account.id,
                account.email,
                True,
                "登录成功",
                previous_sso_token="old-sso",
                sso_token="fresh-sso",
            )

            with patch.object(manager.reference, "sync_grok2api") as sync:
                note = manager._sync_relogin_credentials(result)

            self.assertEqual("", note)
            sync.assert_called_once_with(
                "fresh-sso",
                email="first@example.com",
                log_callback=None,
                previous_token="old-sso",
            )
            stored = manager.store.get(account.id)
            self.assertEqual("fresh-sso", stored.sso_token if stored else "")

    def test_login_persists_credentials_before_external_sync(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            install_fake_login_modules(manager.reference.root, "target@example.com")
            account = manager.store.upsert(
                AccountDraft(
                    email="target@example.com",
                    password="password",
                    sso_token="old-sso",
                )
            )
            observed = []

            def observe_sync(sso_token, email="", log_callback=None, previous_token=""):
                stored = manager.store.get(account.id)
                observed.append(
                    (
                        sso_token,
                        previous_token,
                        stored.sso_token if stored else "",
                        stored.access_token if stored else "",
                    )
                )

            with patch.object(manager.reference, "sync_grok2api", side_effect=observe_sync):
                result = manager.batch_login([account.id])[0]
            stored = manager.store.get(account.id)

            self.assertTrue(result.ok)
            self.assertEqual(
                [("fresh-sso", "old-sso", "fresh-sso", "fresh-access")],
                observed,
            )
            self.assertEqual("fresh-sso", stored.sso_token if stored else "")
            self.assertEqual("fresh-access", stored.access_token if stored else "")

    def test_login_deletes_managed_auth_file_after_successful_sync(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            install_fake_login_modules(manager.reference.root, "target@example.com")
            account = manager.store.upsert(
                AccountDraft(email="target@example.com", password="password")
            )

            result = manager.batch_login([account.id])[0]
            auth_file = manager.reference.managed_auth_dir / "xai-target@example.com.json"

            self.assertTrue(result.ok)
            self.assertEqual("", result.auth_file)
            self.assertFalse(auth_file.exists())

    def test_login_keeps_success_when_grok2api_sync_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            install_fake_login_modules(manager.reference.root, "target@example.com")
            account = manager.store.upsert(
                AccountDraft(
                    email="target@example.com",
                    password="password",
                    sso_token="old-sso",
                )
            )
            auth_file = manager.reference.managed_auth_dir / "xai-target@example.com.json"
            logs = []

            with patch.object(
                manager.reference,
                "sync_grok2api",
                side_effect=RuntimeError("remote pool missing previous credential"),
            ):
                result = manager.batch_login([account.id], log=logs.append)[0]
            stored = manager.store.get(account.id)

            # Login itself still succeeds; remote sync is only annotated.
            self.assertTrue(result.ok)
            self.assertIn("Grok2API 未同步", result.detail)
            self.assertNotEqual(AccountStatus.ERROR.value, stored.status if stored else "")
            self.assertEqual("fresh-sso", stored.sso_token if stored else "")
            self.assertEqual("old-sso", result.previous_sso_token)
            self.assertEqual("fresh-sso", result.sso_token)
            self.assertEqual("fresh-access", stored.access_token if stored else "")
            self.assertFalse(auth_file.exists())
            self.assertTrue(any("Grok2API 未同步" in line for line in logs))
            self.assertTrue(any("部分同步未完成" in line for line in logs))

    def test_login_syncs_cpa_and_grok2api_before_next_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            hotload_dir = root / "cpa-hotload"
            grok2api_file = root / "grok2api-tokens.json"
            manager.reference.config_file.write_text(
                json.dumps(
                    {
                        "cpa_copy_to_hotload": True,
                        "cpa_hotload_dir": str(hotload_dir),
                        "grok2api_auto_add_local": True,
                        "grok2api_local_token_file": str(grok2api_file),
                        "grok2api_pool_name": "ssoBasic",
                        "grok2api_auto_add_remote": False,
                    }
                ),
                encoding="utf-8",
            )
            grok2api_file.write_text(
                json.dumps(
                    {
                        "ssoBasic": [
                            {
                                "token": "old-sso",
                                "tags": ["auto-register"],
                                "note": "first@example.com",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            account = manager.store.upsert(
                AccountDraft(
                    email="first@example.com",
                    password="password",
                    sso_token="old-sso",
                )
            )
            auth_file = root / "xai-first@example.com.json"
            auth_file.write_text(
                json.dumps(
                    {
                        "email": "first@example.com",
                        "access_token": "fresh-access",
                        "refresh_token": "fresh-refresh",
                        "expired": "2099-01-01T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            result_line = "GM_RESULT " + json.dumps(
                {
                    "id": account.id,
                    "email": account.email,
                    "ok": True,
                    "path": str(auth_file),
                    "sso_token": "fresh-sso",
                }
            )
            observed = []

            class ObservingOutput:
                def __iter__(self):
                    yield result_line
                    hotloaded = hotload_dir / auth_file.name
                    pool = json.loads(grok2api_file.read_text(encoding="utf-8"))[
                        "ssoBasic"
                    ]
                    observed.append(
                        (
                            hotloaded.is_file(),
                            [item.get("token") for item in pool],
                        )
                    )

                def close(self):
                    return None

            class FakeProcess:
                stdout = ObservingOutput()

                @staticmethod
                def wait():
                    return 0

            with patch(
                "grok_manager.worker_runtime.subprocess.Popen", return_value=FakeProcess()
            ):
                results = manager.batch_login([account.id])

            self.assertEqual([(True, ["fresh-sso"])], observed)
            self.assertEqual("old-sso", results[0].previous_sso_token)
            self.assertEqual("fresh-sso", results[0].sso_token)
            stored = manager.store.get(account.id)
            self.assertEqual("fresh-sso", stored.sso_token if stored else "")

    def test_worker_runtime_reaps_process_when_parser_raises(self) -> None:
        from grok_manager.worker_runtime import BatchWorkerProcess

        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            waited = []
            terminated = []
            closed = []

            class FakePipe:
                def write(self, _payload):
                    return None

                def close(self):
                    closed.append("stdin")

            class FakeStdout:
                def __iter__(self):
                    yield "GM_RESULT " + json.dumps({"id": 1})

                def close(self):
                    closed.append("stdout")

            class FakeProcess:
                pid = 4242
                stdin = FakePipe()
                stdout = FakeStdout()

                def poll(self):
                    return None if not waited else 1

                def terminate(self):
                    terminated.append("terminate")

                def kill(self):
                    terminated.append("kill")

                def wait(self, timeout=None):
                    waited.append(timeout)
                    return 1

            process = FakeProcess()
            worker = BatchWorkerProcess(
                manager.reference,
                sys.executable,
                job_glob="login-*/input.json",
                busy_error="busy",
            )

            def boom(_payload):
                raise ValueError("parser exploded")

            with patch(
                "grok_manager.worker_runtime.subprocess.Popen", return_value=process
            ), patch("grok_manager.worker_runtime.os.killpg", side_effect=ProcessLookupError):
                with self.assertRaisesRegex(ValueError, "parser exploded"):
                    worker.run(
                        "batch-login",
                        {"settings": {}, "accounts": []},
                        log=lambda _message: None,
                        parse_result=boom,
                        stdin_missing_message="stdin missing",
                        start_failed_message="start failed: %s",
                        exit_failed_message="exit failed: %s",
                    )

            self.assertTrue(waited)
            self.assertIn("terminate", terminated)
            self.assertIn("stdout", closed)
            self.assertIsNone(worker._process)

    def test_login_updates_sso_and_cpa_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            install_fake_login_modules(manager.reference.root, "target@example.com")
            account = manager.store.upsert(
                AccountDraft(
                    email="target@example.com",
                    password="password",
                    sso_token="old-sso",
                    access_token="old-access",
                    refresh_token="old-refresh",
                    auth_file=str(root / "auth" / "xai-target@example.com.json"),
                )
            )

            result = manager.batch_login([account.id])[0]
            stored = manager.store.get(account.id)

            self.assertEqual(
                (
                    True,
                    "fresh-sso",
                    "fresh-access",
                    "fresh-refresh",
                    True,
                    Path(),
                ),
                (
                    result.ok,
                    stored.sso_token if stored else "",
                    stored.access_token if stored else "",
                    stored.refresh_token if stored else "",
                    bool(stored and stored.last_login_at),
                    Path(stored.auth_file).parent if stored else Path(),
                ),
            )


    def test_batch_login_does_not_reset_non_password_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(email="turnstile@example.com", password="password")
            )
            failure = LoginResult(account.id, account.email, False, "安全验证未完成")
            with patch.object(manager.login, "login_accounts", return_value=[failure]):
                with patch.object(manager, "reset_passwords") as reset_passwords:
                    result = manager.batch_login([account.id])[0]

            reset_passwords.assert_not_called()
            self.assertEqual((False, "安全验证未完成"), (result.ok, result.detail))

    def test_batch_login_enables_worker_auto_reset_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(email="wrong-password@example.com", password="old-password")
            )
            recovered = LoginResult(
                account.id,
                account.email,
                True,
                "自动重置密码后：批量登录成功",
            )
            captured = {}
            progress_events = []

            def fake_login(ids, settings, log=None, progress=None):
                captured["ids"] = list(ids)
                captured["settings"] = settings
                if progress:
                    progress(recovered, 1, 1)
                return [recovered]

            with patch.object(manager.login, "login_accounts", side_effect=fake_login):
                with patch.object(manager, "reset_passwords") as reset_passwords:
                    with patch.object(manager, "_sync_relogin_credentials", return_value=""):
                        with patch.object(manager, "inspect_accounts", return_value=[]):
                            results = manager.batch_login(
                                [account.id],
                                progress=lambda result, completed, total: progress_events.append(
                                    (completed, total, result.detail, result.ok)
                                ),
                            )

            self.assertEqual([account.id], captured["ids"])
            self.assertTrue(captured["settings"].auto_reset_password)
            reset_passwords.assert_not_called()
            self.assertTrue(results[0].ok)
            self.assertIn("自动重置密码后", results[0].detail)
            self.assertEqual((1, 1, True), (progress_events[-1][0], progress_events[-1][1], progress_events[-1][3]))

    def test_batch_login_worker_settled_recovery_advances_progress_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            good = manager.store.upsert(
                AccountDraft(email="good@example.com", password="password")
            )
            bad = manager.store.upsert(
                AccountDraft(email="bad@example.com", password="old-password")
            )
            good_result = LoginResult(good.id, good.email, True, "批量登录成功")
            recovered = LoginResult(
                bad.id, bad.email, True, "自动重置密码后：批量登录成功"
            )
            progress_events = []
            login_calls = []

            def fake_login(ids, settings, log=None, progress=None):
                login_calls.append(list(ids))
                self.assertTrue(settings.auto_reset_password)
                # Worker emits only final settled results; recovery is internal.
                if progress:
                    progress(good_result, 1, 2)
                    progress(recovered, 2, 2)
                return [good_result, recovered]

            with patch.object(manager.login, "login_accounts", side_effect=fake_login):
                with patch.object(manager, "reset_passwords") as reset_passwords:
                    with patch.object(manager, "_sync_relogin_credentials", return_value=""):
                        with patch.object(manager, "inspect_accounts", return_value=[]):
                            results = manager.batch_login(
                                [good.id, bad.id],
                                progress=lambda result, completed, total: progress_events.append(
                                    (completed, total, result.email, result.ok, result.detail)
                                ),
                            )

            reset_passwords.assert_not_called()
            self.assertEqual(1, len(login_calls))
            self.assertEqual(2, progress_events[-1][0])
            self.assertEqual(2, progress_events[-1][1])
            self.assertTrue(all(item.ok for item in results))

    def test_batch_login_skips_worker_when_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(email="cancel-reset@example.com", password="old-password")
            )
            progress_events = []

            with patch.object(manager.login, "login_accounts") as login_accounts:
                with patch.object(manager, "reset_passwords") as reset_passwords:
                    with patch.object(manager, "inspect_accounts", return_value=[]):
                        results = manager.batch_login(
                            [account.id],
                            progress=lambda result, completed, total: progress_events.append(
                                (completed, total, result.detail)
                            ),
                            cancelled=lambda: True,
                        )

            login_accounts.assert_not_called()
            reset_passwords.assert_not_called()
            self.assertFalse(results[0].ok)
            self.assertIn("任务已取消", results[0].detail)
            self.assertEqual(1, progress_events[-1][0])

    def test_batch_login_preserves_mixed_worker_recovery_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            one = manager.store.upsert(
                AccountDraft(email="one@example.com", password="old")
            )
            two = manager.store.upsert(
                AccountDraft(email="two@example.com", password="old")
            )
            first_fail = LoginResult(
                one.id,
                one.email,
                False,
                "邮箱或密码错误；自动重置密码失败：reset worker boom",
            )
            second_ok = LoginResult(
                two.id, two.email, True, "自动重置密码后：批量登录成功"
            )

            def fake_login(ids, settings, log=None, progress=None):
                self.assertTrue(settings.auto_reset_password)
                values = [first_fail, second_ok]
                for index, result in enumerate(values, start=1):
                    if progress:
                        progress(result, index, 2)
                return values

            with patch.object(manager.login, "login_accounts", side_effect=fake_login):
                with patch.object(manager, "reset_passwords") as reset_passwords:
                    with patch.object(manager, "_sync_relogin_credentials", return_value=""):
                        with patch.object(manager, "inspect_accounts", return_value=[]):
                            results = manager.batch_login([one.id, two.id])

            reset_passwords.assert_not_called()
            by_email = {item.email: item for item in results}
            self.assertFalse(by_email[one.email].ok)
            self.assertIn("自动重置密码失败", by_email[one.email].detail)
            self.assertTrue(by_email[two.email].ok)

    def test_batch_login_disables_auto_reset_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(email="variant@example.com", password="old-password")
            )
            first = LoginResult(
                account.id,
                account.email,
                False,
                "The email or password you entered is incorrect.",
            )
            captured = {}
            with patch.object(
                manager.login,
                "login_accounts",
                side_effect=lambda ids, settings, log=None, progress=None: (
                    captured.update({"settings": settings}) or [first]
                ),
            ):
                with patch.object(manager, "reset_passwords") as reset_passwords:
                    with patch.object(manager, "inspect_accounts", return_value=[]):
                        results = manager.batch_login(
                            [account.id], auto_reset_password=False
                        )

            self.assertFalse(captured["settings"].auto_reset_password)
            reset_passwords.assert_not_called()
            self.assertFalse(results[0].ok)
            self.assertEqual(first.detail, results[0].detail)

    def test_pending_sso_replace_is_remembered_across_sync_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(
                    email="pending@example.com",
                    password="password",
                    sso_token="fresh-sso",
                    access_token="access",
                    refresh_token="refresh",
                )
            )
            first = LoginResult(
                account.id,
                account.email,
                True,
                "登录成功",
                previous_sso_token="old-sso",
                sso_token="fresh-sso",
            )
            second = LoginResult(
                account.id,
                account.email,
                True,
                "登录成功",
                previous_sso_token="fresh-sso",
                sso_token="newer-sso",
            )
            seen = []

            def fail_sync(sso_token, email="", log_callback=None, previous_token=""):
                seen.append((previous_token, sso_token))
                raise RuntimeError("remote unavailable")

            with patch.object(manager.reference, "sync_grok2api", side_effect=fail_sync):
                note1 = manager._sync_relogin_credentials(first)
                note2 = manager._sync_relogin_credentials(second)

            self.assertIn("Grok2API 未同步", note1)
            self.assertIn("Grok2API 未同步", note2)
            # Generic remote failures do not burn through the whole candidate
            # chain; only explicit account_not_found advances to pending_new.
            self.assertEqual(
                [
                    ("old-sso", "fresh-sso"),
                    ("old-sso", "newer-sso"),
                ],
                seen,
            )
            self.assertEqual(
                "old-sso\nnewer-sso",
                manager.vault.get_secret("pending-sso-replace:pending@example.com"),
            )

    def test_pending_sso_chain_recovers_after_lost_replace_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(
                    email="recover@example.com",
                    password="password",
                    sso_token="fresh-sso",
                    access_token="access",
                    refresh_token="refresh",
                )
            )
            # First login persisted pending old->fresh after a lost response.
            manager.vault.put_secret(
                "pending-sso-replace:recover@example.com", "old-sso\nfresh-sso"
            )
            result = LoginResult(
                account.id,
                account.email,
                True,
                "登录成功",
                previous_sso_token="fresh-sso",
                sso_token="newer-sso",
            )
            seen = []

            def sync(sso_token, email="", log_callback=None, previous_token=""):
                seen.append((previous_token, sso_token))
                if previous_token == "old-sso":
                    raise RuntimeError(
                        "grok2api 远端未找到待替换凭据，已拒绝新增: recover@example.com"
                    )
                if previous_token == "fresh-sso" and sso_token == "newer-sso":
                    return None
                raise RuntimeError("unexpected previous token: %s" % previous_token)

            with patch.object(manager.reference, "sync_grok2api", side_effect=sync):
                note = manager._sync_relogin_credentials(result)

            self.assertEqual("", note)
            self.assertEqual(
                [("old-sso", "newer-sso"), ("fresh-sso", "newer-sso")],
                seen,
            )
            self.assertEqual(
                "",
                manager.vault.get_secret("pending-sso-replace:recover@example.com"),
            )

    def test_same_token_relogin_keeps_previous_token_for_remote_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(
                    email="same@example.com",
                    password="password",
                    sso_token="same-sso",
                    access_token="access",
                    refresh_token="refresh",
                )
            )
            result = LoginResult(
                account.id,
                account.email,
                True,
                "登录成功",
                previous_sso_token="same-sso",
                sso_token="same-sso",
            )
            seen = []

            def sync(sso_token, email="", log_callback=None, previous_token=""):
                seen.append((previous_token, sso_token))
                return None

            with patch.object(manager.reference, "sync_grok2api", side_effect=sync):
                note = manager._sync_relogin_credentials(result)

            self.assertEqual("", note)
            self.assertEqual([("same-sso", "same-sso")], seen)

    def test_pending_chain_persists_after_failed_intermediate_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(
                    email="chain@example.com",
                    password="password",
                    sso_token="fresh-sso",
                    access_token="access",
                    refresh_token="refresh",
                )
            )
            manager.vault.put_secret(
                "pending-sso-replace:chain@example.com", "old-sso\nfresh-sso"
            )
            result = LoginResult(
                account.id,
                account.email,
                True,
                "登录成功",
                previous_sso_token="fresh-sso",
                sso_token="newer-sso",
            )
            seen = []

            def sync(sso_token, email="", log_callback=None, previous_token=""):
                seen.append((previous_token, sso_token))
                if previous_token == "old-sso":
                    raise RuntimeError(
                        "grok2api 远端未找到待替换凭据，已拒绝新增: chain@example.com"
                    )
                raise RuntimeError("remote unavailable after advance")

            with patch.object(manager.reference, "sync_grok2api", side_effect=sync):
                note = manager._sync_relogin_credentials(result)

            self.assertIn("Grok2API 未同步", note)
            self.assertEqual(
                [("old-sso", "newer-sso"), ("fresh-sso", "newer-sso")],
                seen,
            )
            # Intermediate token must remain as the next previous candidate.
            self.assertEqual(
                "fresh-sso\nnewer-sso",
                manager.vault.get_secret("pending-sso-replace:chain@example.com"),
            )

    def test_batch_login_reports_missing_ids_and_preserves_request_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            first = manager.store.upsert(
                AccountDraft(email="first@example.com", password="password")
            )
            second = manager.store.upsert(
                AccountDraft(email="second@example.com", password="password")
            )
            first_result = LoginResult(first.id, first.email, True, "批量登录成功")
            second_result = LoginResult(second.id, second.email, True, "批量登录成功")
            progress_events = []

            def fake_login(_ids, _settings, log=None, progress=None):
                for index, result in enumerate((first_result, second_result), start=1):
                    if progress:
                        progress(result, index, 2)
                return [first_result, second_result]

            missing_id = 999999
            with patch.object(manager.login, "login_accounts", side_effect=fake_login):
                with patch.object(manager, "_sync_relogin_credentials", return_value=""):
                    with patch.object(manager, "inspect_accounts", return_value=[]):
                        results = manager.batch_login(
                            [second.id, missing_id, first.id, second.id],
                            progress=lambda result, completed, total: progress_events.append(
                                (result.account_id, completed, total)
                            ),
                        )

            self.assertEqual([second.id, missing_id, first.id], [item.account_id for item in results])
            self.assertEqual([True, False, True], [item.ok for item in results])
            self.assertEqual("账号不存在", results[1].detail)
            self.assertEqual((3, 3), progress_events[-1][1:])

    def test_wrong_password_recovery_is_delegated_to_single_login_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            one = manager.store.upsert(
                AccountDraft(email="batch-one@example.com", password="old")
            )
            two = manager.store.upsert(
                AccountDraft(email="batch-two@example.com", password="old")
            )
            recovered = {
                one.id: LoginResult(
                    one.id, one.email, True, "自动重置密码后：批量登录成功"
                ),
                two.id: LoginResult(
                    two.id, two.email, True, "自动重置密码后：批量登录成功"
                ),
            }
            login_calls = []

            def fake_login(ids, settings, log=None, progress=None):
                requested = list(ids)
                login_calls.append(requested)
                self.assertTrue(settings.auto_reset_password)
                values = [recovered[account_id] for account_id in requested]
                for index, result in enumerate(values, start=1):
                    if progress:
                        progress(result, index, len(values))
                return values

            with patch.object(manager.login, "login_accounts", side_effect=fake_login):
                with patch.object(manager, "reset_passwords") as reset:
                    with patch.object(manager, "_sync_relogin_credentials", return_value=""):
                        with patch.object(manager, "inspect_accounts", return_value=[]):
                            results = manager.batch_login([two.id, one.id])

            self.assertEqual([[two.id, one.id]], login_calls)
            reset.assert_not_called()
            self.assertEqual([two.id, one.id], [item.account_id for item in results])
            self.assertTrue(all(item.ok for item in results))

    def test_stale_auth_file_is_not_published_to_hotload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(
                    email="stale-publish@example.com",
                    access_token="current-access",
                    refresh_token="current-refresh",
                )
            )
            auth_path = manager.reference.managed_auth_dir / "xai-stale-publish@example.com.json"
            auth_path.parent.mkdir(parents=True, exist_ok=True)
            auth_path.write_text(
                json.dumps(
                    {
                        "email": account.email,
                        "access_token": "stale-access",
                        "refresh_token": "stale-refresh",
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(manager.reference, "sync_cpa_hotload") as sync:
                hotload_path = manager._sync_hotload_path(auth_path, account.id)

            self.assertIsNone(hotload_path)
            sync.assert_not_called()
            manager._remove_managed_auth_file(auth_path)


    def test_remint_success_discards_result_after_concurrent_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(
                    email="remint-cas@example.com",
                    access_token="old-access",
                    refresh_token="stable-refresh",
                )
            )
            snapshot = manager.store.get(account.id)
            manager.login._remint_expected_snapshot = {
                account.id: {
                    "access": snapshot.access_token if snapshot else "",
                    "refresh": snapshot.refresh_token if snapshot else "",
                    "cpa_updated_at": snapshot.cpa_updated_at if snapshot else "",
                }
            }
            auth_path = manager.reference.managed_auth_dir / "xai-remint-cas@example.com.json"
            auth_path.parent.mkdir(parents=True, exist_ok=True)
            auth_path.write_text(
                json.dumps(
                    {
                        "email": account.email,
                        "access_token": "stale-remint-access",
                        "refresh_token": "stale-remint-refresh",
                    }
                ),
                encoding="utf-8",
            )
            manager.store.apply_cpa_credentials(
                account.id,
                "concurrent-access",
                "stable-refresh",
                "2099-01-01T00:00:00Z",
                "",
            )

            result = manager.login._handle_remint_result(
                json.dumps(
                    {
                        "id": account.id,
                        "email": account.email,
                        "ok": True,
                        "path": str(auth_path),
                    }
                )
            )

            stored = manager.store.get(account.id)
            self.assertTrue(result.ok)
            self.assertIn("并发任务更新", result.detail)
            self.assertEqual("concurrent-access", stored.access_token if stored else "")
            self.assertFalse(auth_path.exists())



class ImmediateWrongPasswordRecoveryTests(unittest.TestCase):
    def test_is_wrong_password_error_only_canonical(self) -> None:
        from grok_manager.reference_worker import is_wrong_password_error

        self.assertTrue(is_wrong_password_error("邮箱或密码错误"))
        self.assertTrue(is_wrong_password_error("前缀：邮箱或密码错误"))
        self.assertFalse(is_wrong_password_error("The email or password you entered is incorrect."))
        self.assertFalse(is_wrong_password_error("安全验证未完成"))

    def test_recover_wrong_password_resets_then_relogins_once(self) -> None:
        from grok_manager import reference_worker

        item = {
            "id": 7,
            "email": "bad@example.com",
            "password": "old",
            "mail_credential": "jwt",
            "auth_dir": "/tmp",
        }
        settings = {"default_auth_dir": "/tmp", "timeout_seconds": 60}
        calls = {"login": 0, "reset": 0}
        events = []

        def fake_emit(prefix, payload):
            events.append((prefix.strip(), dict(payload)))

        def fake_reset(item_arg, settings_arg, log):
            calls["reset"] += 1
            self.assertEqual("jwt", item_arg["mail_credential"])
            return {"ok": True, "password": "new-secret-password"}

        def fake_login(item_arg, settings_arg, mint_and_export, log=None):
            calls["login"] += 1
            self.assertEqual("new-secret-password", item_arg["password"])
            return {
                "ok": True,
                "id": item_arg["id"],
                "email": item_arg["email"],
                "path": "/tmp/auth.json",
                "sso_token": "sso",
            }

        with patch.object(reference_worker, "emit", side_effect=fake_emit):
            with patch.object(reference_worker, "password_reset_core", side_effect=fake_reset):
                with patch.object(reference_worker, "login_item_core", side_effect=fake_login):
                    result = reference_worker.recover_wrong_password_item(
                        item, settings, mint_and_export=object()
                    )

        self.assertEqual(1, calls["reset"])
        self.assertEqual(1, calls["login"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["recovered_from_wrong_password"])
        self.assertEqual("new-secret-password", result["password"])
        self.assertIn("自动重置密码后", result["detail"])
        logs = [
            payload.get("message")
            for prefix, payload in events
            if prefix == "GM_LOG"
        ]
        self.assertTrue(any("开始自动重置密码" in str(message) for message in logs))
        self.assertTrue(any("开始使用新密码重新登录" in str(message) for message in logs))

    def test_run_login_batch_recovers_wrong_password_inline(self) -> None:
        from grok_manager import reference_worker

        item = {
            "id": 3,
            "email": "bad@example.com",
            "password": "old",
            "mail_credential": "jwt",
            "auth_dir": "/tmp",
        }
        settings = {"default_auth_dir": "/tmp"}
        emitted = []
        recover_calls = []

        def fake_login(item_arg, settings_arg, mint_and_export, log=None):
            return {"ok": False, "error": "邮箱或密码错误", "id": 3, "email": "bad@example.com"}

        def fake_recover(item_arg, settings_arg, mint_and_export):
            recover_calls.append(item_arg["id"])
            return {
                "ok": True,
                "id": 3,
                "email": "bad@example.com",
                "detail": "自动重置密码后：批量登录成功",
                "recovered_from_wrong_password": True,
                "password": "new-secret",
            }

        with patch.object(reference_worker, "login_item_core", side_effect=fake_login):
            with patch.object(reference_worker, "recover_wrong_password_item", side_effect=fake_recover):
                with patch.object(reference_worker, "emit", side_effect=lambda p, v: emitted.append((p, v))):
                    with patch.object(reference_worker, "shutdown_thread_browsers"):
                        results = reference_worker.run_login_batch(
                            [item],
                            settings,
                            mint_and_export=object(),
                            auto_reset_password=True,
                        )

        self.assertEqual([3], recover_calls)
        self.assertEqual(1, len(results))
        self.assertTrue(results[0]["ok"])
        self.assertTrue(any(prefix.startswith("GM_RESULT") for prefix, _ in emitted))

    def test_login_payload_includes_mail_credential_when_auto_reset_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(email="mail@example.com", password="password", source="src")
            )
            captured = {}

            def fake_run(command, document, **kwargs):
                captured["document"] = document
                return [], set(), 0

            with patch.object(manager.login._worker, "run", side_effect=fake_run):
                with patch.object(
                    manager.login.project,
                    "find_mail_credential",
                    return_value="mail-jwt",
                ):
                    manager.login.login_accounts(
                        [account.id],
                        LoginSettings(auto_reset_password=True),
                    )

            settings = captured["document"]["settings"]
            account_payload = captured["document"]["accounts"][0]
            self.assertTrue(settings["auto_reset_password"])
            self.assertEqual("mail-jwt", account_payload["mail_credential"])


if __name__ == "__main__":
    unittest.main()
