import tempfile
import textwrap
import unittest
from pathlib import Path

from grok_manager.config import ConfigStore, ManagerConfig
from grok_manager.paths import PROJECT_ROOT
from grok_manager.reference import RegistrationRequest
from grok_manager.service import GrokManager
from grok_manager.store import AccountStore
from tests.support import make_manager


class RegistrationImportTests(unittest.TestCase):
    def test_default_registration_runtime_is_embedded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_store = ConfigStore(root / "manager-config.json")
            config_store.save(ManagerConfig(auto_import_on_start=False))

            manager = GrokManager(
                config_store=config_store,
                store=AccountStore(root / "accounts.sqlite3"),
            )

            self.assertEqual(
                (PROJECT_ROOT, True, ""),
                (
                    manager.reference.root,
                    manager.reference.config_file.is_file(),
                    manager.reference.load_registration_config().get("proxy"),
                ),
            )

    def test_registration_without_accounts_is_not_successful(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            manager.reference.entrypoint.write_text(
                textwrap.dedent(
                    """
                    import argparse
                    from pathlib import Path

                    parser = argparse.ArgumentParser()
                    parser.add_argument("--count")
                    parser.add_argument("--threads")
                    parser.add_argument("--mint-workers")
                    parser.add_argument("--accounts-file", required=True)
                    args = parser.parse_args()
                    Path(args.accounts_file).write_text("", encoding="utf-8")
                    batch = Path(__file__).parent / "output" / "out_empty"
                    batch.mkdir(parents=True, exist_ok=True)
                    print("[*] 本次批次目录 = %s" % batch, flush=True)
                    """
                ),
                encoding="utf-8",
            )

            result = manager.run_registration(RegistrationRequest(1, 1, 0))

            self.assertFalse(result.ok)

    def test_registration_imports_sso_and_cpa_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            manager.reference.entrypoint.write_text(
                textwrap.dedent(
                    """
                    import argparse
                    import json
                    from pathlib import Path

                    parser = argparse.ArgumentParser()
                    parser.add_argument("--count")
                    parser.add_argument("--threads")
                    parser.add_argument("--mint-workers")
                    parser.add_argument("--accounts-file", required=True)
                    args = parser.parse_args()
                    Path(args.accounts_file).write_text(
                        "registered@example.com----password----fresh-sso\\n",
                        encoding="utf-8",
                    )
                    batch = Path(__file__).parent / "output" / "out_success"
                    auth_dir = batch / "cpa_auths"
                    auth_dir.mkdir(parents=True, exist_ok=True)
                    (auth_dir / "xai-registered@example.com.json").write_text(
                        json.dumps(
                            {
                                "email": "registered@example.com",
                                "access_token": "fresh-access",
                                "refresh_token": "fresh-refresh",
                                "expired": "2099-01-01T00:00:00Z",
                            }
                        ),
                        encoding="utf-8",
                    )
                    print("[*] 本次批次目录 = %s" % batch, flush=True)
                    """
                ),
                encoding="utf-8",
            )

            result = manager.run_registration(RegistrationRequest(1, 1, 1))
            account = manager.store.get_by_email("registered@example.com")

            self.assertEqual(
                (True, 1, "fresh-sso", "fresh-access", "fresh-refresh"),
                (
                    result.ok,
                    result.imported_count,
                    account.sso_token if account else "",
                    account.access_token if account else "",
                    account.refresh_token if account else "",
                ),
            )


if __name__ == "__main__":
    unittest.main()
