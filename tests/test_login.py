import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import DrissionPage
from grok_manager.models import AccountDraft
from grok_manager.paths import MANAGED_AUTH_DIR
from grok_register.cpa_xai import browser_confirm
from grok_register.paths import TURNSTILE_DIR
from tests.support import make_manager


def install_fake_login_modules(reference_root: Path, auth_email: str) -> None:
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
            set_sso("fresh-sso")
            return {"ok": True, "path": str(path)}
        """
    ).replace("__AUTH_EMAIL__", repr(auth_email))
    (package / "mint.py").write_text(
        mint_source,
        encoding="utf-8",
    )


class BatchLoginCredentialTests(unittest.TestCase):
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
                (True, "fresh-sso", "fresh-access", "fresh-refresh", True, MANAGED_AUTH_DIR),
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
