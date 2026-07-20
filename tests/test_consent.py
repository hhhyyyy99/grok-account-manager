from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from grok_manager.models import AccountDraft, ConsentResult
from grok_manager.web import GrokWebApplication
from tests.support import make_manager


class ConsentServiceTests(unittest.TestCase):
    def test_consent_accounts_requires_sso(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(email="no-sso@example.com", password="password")
            )
            result = manager.consent_accounts([account.id])[0]
            self.assertFalse(result.ok)
            self.assertIn("SSO", result.detail)

    def test_consent_accounts_runs_for_ready_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(
                    email="ready@example.com",
                    password="password",
                    sso_token="sso-token-value",
                )
            )
            fake = ConsentResult(
                account.id,
                account.email,
                True,
                "TOS 门禁已通过",
                tos_ok=True,
            )
            with patch.object(
                manager.consent,
                "consent_accounts",
                return_value=[fake],
            ) as consent:
                results = manager.consent_accounts([account.id])
            consent.assert_called_once()
            self.assertEqual(1, len(results))
            self.assertTrue(results[0].ok)
            self.assertTrue(results[0].tos_ok)

    def test_batch_login_does_not_rerun_consent_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(email="login-consent@example.com", password="password")
            )
            login_result = __import__(
                "grok_manager.models", fromlist=["LoginResult"]
            ).LoginResult(account.id, account.email, True, "批量登录成功")
            with patch.object(
                manager.login, "login_accounts", return_value=[login_result]
            ), patch.object(
                manager, "_sync_relogin_credentials", return_value=""
            ), patch.object(
                manager, "consent_accounts"
            ) as consent, patch.object(
                manager, "inspect_accounts", return_value=[]
            ):
                result = manager.batch_login([account.id])[0]
            # TOS runs inside mint browser before Build authorize; no second pass.
            consent.assert_not_called()
            self.assertTrue(result.ok)
            self.assertIn("批量登录成功", result.detail)

    def test_web_start_consent_creates_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(
                    email="web-consent@example.com",
                    password="password",
                    sso_token="sso",
                )
            )
            application = GrokWebApplication(manager)
            fake = ConsentResult(
                account.id, account.email, True, "ok", tos_ok=True
            )
            with patch.object(manager, "consent_accounts", return_value=[fake]):
                task = application.start_consent({"ids": [account.id]})
                for _ in range(50):
                    if task.state in {"succeeded", "failed", "partial", "cancelled"}:
                        break
                    time.sleep(0.02)
            self.assertEqual("consent", task.kind)
            self.assertEqual("授权确认", task.label)
            self.assertIn(task.state, {"succeeded", "partial", "failed"})





    def test_login_settings_forward_require_account_gates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(email="gates-flag@example.com", password="password")
            )
            captured = {}

            def fake_login(ids, settings, log=None, progress=None, cancelled=None):
                captured["require_account_gates"] = bool(settings.require_account_gates)
                result = __import__("grok_manager.models", fromlist=["LoginResult"]).LoginResult(
                    account.id, account.email, True, "批量登录成功"
                )
                if progress:
                    progress(result, 1, 1)
                return [result]

            with patch.object(manager.login, "login_accounts", side_effect=fake_login), patch.object(
                manager, "_sync_relogin_credentials", return_value=""
            ), patch.object(manager, "inspect_accounts", return_value=[]):
                manager.batch_login([account.id], require_account_gates=False)
                self.assertFalse(captured["require_account_gates"])
                manager.batch_login([account.id], require_account_gates=True)
                self.assertTrue(captured["require_account_gates"])


class GateDetectionTests(unittest.TestCase):
    def test_cloudflare_page_is_not_pass(self) -> None:
        from grok_register.cpa_xai import browser_confirm as bc
        self.assertTrue(bc.looks_like_cloudflare("https://grok.com/", "Just a moment..."))
        self.assertFalse(bc.looks_like_grok_app("https://grok.com/", "Just a moment..."))

    def test_tos_gate_detection(self) -> None:
        from grok_register.cpa_xai import browser_confirm as bc
        self.assertTrue(bc.looks_like_tos_gate("https://grok.com/tos-gate", "知道了"))
        self.assertFalse(bc.looks_like_tos_gate("https://grok.com/", "新建聊天"))

    def test_app_markers(self) -> None:
        from grok_register.cpa_xai import browser_confirm as bc
        self.assertTrue(bc.looks_like_grok_app("https://grok.com/", "新建聊天 你想知道什么"))


    def test_page_cf_clearance_prefers_grok_domain(self) -> None:
        from grok_register.cpa_xai import browser_confirm as bc
        class FakePage:
            def cookies(self):
                return [
                    {"name": "cf_clearance", "value": "other", "domain": ".example.com"},
                    {"name": "cf_clearance", "value": "grok-pass", "domain": ".grok.com"},
                    {"name": "sso", "value": "x", "domain": ".grok.com"},
                ]
        self.assertEqual("grok-pass", bc._page_cf_clearance(FakePage()))

    def test_page_cf_clearance_empty_without_cookie(self) -> None:
        from grok_register.cpa_xai import browser_confirm as bc
        class FakePage:
            def cookies(self):
                return [{"name": "sso", "value": "x", "domain": ".grok.com"}]
        self.assertEqual("", bc._page_cf_clearance(FakePage()))

if __name__ == "__main__":
    unittest.main()
