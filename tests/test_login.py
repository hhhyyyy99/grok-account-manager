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
from grok_manager.models import AccountDraft, AccountStatus, LoginResult, PasswordResetResult
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
            "密码不正确",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                with self.assertRaisesRegex(
                    browser_confirm.BrowserConfirmError, "邮箱或密码错误"
                ):
                    browser_confirm._raise_for_login_error(sample)

    def test_password_page_retries_surface_wrong_password_error(self) -> None:
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
                    return "Sign in"
                return ""

        page = FakePage()
        logs = []

        with patch.object(browser_confirm, "_wait_turnstile", return_value=True), patch.object(
            browser_confirm, "_click_exact", return_value=True
        ), patch.object(browser_confirm, "_sleep", return_value=None), patch.object(
            browser_confirm, "_page_url", return_value="https://accounts.x.ai/sign-in"
        ), patch.object(browser_confirm, "_click_email_login_chooser", return_value=False):
            with self.assertRaisesRegex(browser_confirm.BrowserConfirmError, "邮箱或密码错误"):
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
                    sso_token="old-sso",
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
                manager._sync_relogin_credentials(result)

            sync.assert_called_once_with(
                "fresh-sso",
                email="first@example.com",
                log_callback=None,
                previous_token="old-sso",
            )
            stored = manager.store.get(account.id)
            self.assertEqual("fresh-sso", stored.sso_token if stored else "")

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

    def test_login_marks_error_when_grok2api_sync_fails(self) -> None:
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

            self.assertFalse(result.ok)
            self.assertIn("凭据同步失败", result.detail)
            self.assertEqual(AccountStatus.ERROR.value, stored.status if stored else "")
            # Keep the previous SSO so the next relogin can still match remote.
            self.assertEqual("old-sso", stored.sso_token if stored else "")
            self.assertEqual("old-sso", result.previous_sso_token)
            self.assertEqual("fresh-sso", result.sso_token)
            self.assertEqual("fresh-access", stored.access_token if stored else "")
            self.assertFalse(auth_file.exists())
            self.assertTrue(any("Grok2API 更新失败" in line for line in logs))

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

    def test_batch_login_auto_resets_wrong_password_and_retries_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(email="wrong-password@example.com", password="old-password")
            )
            first = LoginResult(account.id, account.email, False, "邮箱或密码错误")
            retried = LoginResult(account.id, account.email, True, "批量登录成功")
            reset = PasswordResetResult(account.id, account.email, True, "密码已重置")
            calls = []

            def fake_login(_ids, _settings, log=None, progress=None):
                calls.append(list(_ids))
                return [first] if len(calls) == 1 else [retried]

            logs = []
            with patch.object(manager.login, "login_accounts", side_effect=fake_login):
                with patch.object(manager, "reset_passwords", return_value=[reset]) as reset_passwords:
                    with patch.object(manager, "_sync_relogin_credentials"):
                        with patch.object(manager, "inspect_accounts", return_value=[]):
                            results = manager.batch_login([account.id], log=logs.append)

            self.assertEqual(2, len(calls))
            self.assertEqual([[account.id], [account.id]], calls)
            reset_passwords.assert_called_once_with([account.id], log=logs.append)
            self.assertEqual(True, results[0].ok)
            self.assertIn("自动重置密码后", results[0].detail)
            self.assertTrue(any("邮箱或密码错误" in line for line in logs))

    def test_batch_login_auto_resets_variant_password_error_messages(self) -> None:
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
            retried = LoginResult(account.id, account.email, True, "批量登录成功")
            reset = PasswordResetResult(account.id, account.email, True, "密码已重置")
            calls = []

            def fake_login(_ids, _settings, log=None, progress=None):
                calls.append(list(_ids))
                return [first] if len(calls) == 1 else [retried]

            logs = []
            with patch.object(manager.login, "login_accounts", side_effect=fake_login):
                with patch.object(manager, "reset_passwords", return_value=[reset]) as reset_passwords:
                    with patch.object(manager, "_sync_relogin_credentials"):
                        with patch.object(manager, "inspect_accounts", return_value=[]):
                            results = manager.batch_login([account.id], log=logs.append)

            self.assertEqual(2, len(calls))
            reset_passwords.assert_called_once_with([account.id], log=logs.append)
            self.assertTrue(results[0].ok)
            self.assertTrue(any("自动重置密码" in line for line in logs))

if __name__ == "__main__":
    unittest.main()
