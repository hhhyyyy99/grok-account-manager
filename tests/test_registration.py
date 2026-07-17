import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from grok_manager.config import ConfigStore, ManagerConfig
from grok_manager.paths import PROJECT_ROOT
from grok_manager.reference import (
    ReferenceProject,
    RegistrationRequest,
    _is_supported_python,
)
from grok_manager.service import GrokManager
from grok_manager.store import AccountStore
from tests.support import make_manager


class RegistrationImportTests(unittest.TestCase):
    def test_embedded_runtime_requires_python_3_13(self) -> None:
        self.assertTrue(_is_supported_python("3.13.13"))
        self.assertFalse(_is_supported_python("3.12.11"))
        self.assertFalse(_is_supported_python("3.14.0"))

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

    def test_legacy_project_is_migrated_once_to_local_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            embedded_root = root / "embedded"
            package = embedded_root / "grok_register"
            (package / "turnstilePatch").mkdir(parents=True)
            (package / "cli.py").write_text("", encoding="utf-8")
            (package / "turnstilePatch" / "manifest.json").write_text(
                "{}\n", encoding="utf-8"
            )
            example = embedded_root / "registration-config.example.json"
            example.write_text('{"proxy": ""}\n', encoding="utf-8")

            legacy_root = root / "grok-register-mint"
            legacy_auth = legacy_root / "output" / "out_old" / "cpa_auths"
            legacy_auth.mkdir(parents=True)
            (legacy_root / "config.json").write_text(
                json.dumps(
                    {
                        "proxy": "http://127.0.0.1:7890",
                        "cpa_auth_dir": str(legacy_auth),
                    }
                ),
                encoding="utf-8",
            )
            (legacy_root / "output" / "out_old" / "accounts.txt").write_text(
                "old@example.com----password----old-sso\n",
                encoding="utf-8",
            )
            (legacy_auth / "xai-old@example.com.json").write_text(
                json.dumps(
                    {
                        "email": "old@example.com",
                        "access_token": "old-access",
                    }
                ),
                encoding="utf-8",
            )
            legacy_manager_config = root / "legacy-manager-config.json"
            legacy_manager_config.write_text(
                json.dumps({"reference_project": str(legacy_root)}),
                encoding="utf-8",
            )

            data_root = root / "data"
            registration_config = data_root / "registration-config.json"
            migration_file = data_root / ".migration.json"
            reference = ReferenceProject(
                root=embedded_root,
                config_file=registration_config,
                config_example_file=example,
                output_dir=data_root / "registration-output",
                data_root=data_root,
                legacy_manager_config_file=legacy_manager_config,
                migration_file=migration_file,
            )
            manager = GrokManager(
                config_store=ConfigStore(data_root / "manager-config.json"),
                store=AccountStore(data_root / "accounts.sqlite3"),
                reference=reference,
            )

            migrated_config = manager.reference.load_registration_config()
            records = manager.reference.import_records()
            self.assertEqual("http://127.0.0.1:7890", migrated_config["proxy"])
            self.assertNotIn(str(legacy_root), migrated_config["cpa_auth_dir"])
            self.assertEqual(
                (True, ["old@example.com"], "old-access"),
                (
                    migration_file.is_file(),
                    [record.email for record in records],
                    records[0].access_token,
                ),
            )

            (legacy_root / "config.json").write_text(
                '{"proxy": "changed"}\n', encoding="utf-8"
            )
            manager.reference.migrate_legacy_data()
            self.assertEqual(
                "http://127.0.0.1:7890",
                manager.reference.load_registration_config()["proxy"],
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
