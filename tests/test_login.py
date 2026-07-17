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
from grok_manager.models import AccountDraft, AccountStatus
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
        with self.assertRaisesRegex(browser_confirm.BrowserConfirmError, "邮箱或密码错误"):
            browser_confirm._raise_for_login_error("Wrong email address or password.")

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
                "grok_manager.login.subprocess.Popen", return_value=FakeProcess()
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
                    MANAGED_AUTH_DIR.resolve(),
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


if __name__ == "__main__":
    unittest.main()
