import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from grok_manager.config import ConfigStore, ManagerConfig
from grok_manager.models import AccountDraft
from grok_manager.paths import PROJECT_ROOT
from grok_manager.reference import (
    ReferenceProject,
    RegistrationRequest,
    _is_supported_python,
)
from grok_manager.vault import CredentialVault, KdfParameters
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

            vault = CredentialVault(
                root / "credentials.vault.json",
                kdf_parameters=KdfParameters(memory_cost=8 * 1024, iterations=1, lanes=1),
            )
            vault.initialize("test vault password 123")
            manager = GrokManager(
                config_store=config_store,
                store=AccountStore(root / "accounts.sqlite3", vault=vault),
            )

            self.assertEqual(
                (PROJECT_ROOT, True),
                (
                    manager.reference.root,
                    manager.reference.config_file.is_file(),
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
            (legacy_auth / "xai-database@example.com.json").write_text(
                json.dumps(
                    {
                        "email": "database@example.com",
                        "access_token": "database-access",
                    }
                ),
                encoding="utf-8",
            )
            legacy_manager_config = root / "legacy-manager-config.json"
            legacy_manager_config.write_text(
                json.dumps({"reference_project": legacy_root.name}),
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
            vault = CredentialVault(
                data_root / "credentials.vault.json",
                kdf_parameters=KdfParameters(memory_cost=8 * 1024, iterations=1, lanes=1),
            )
            vault.initialize("test vault password 123")
            store = AccountStore(data_root / "accounts.sqlite3", vault=vault)
            stored_before_migration = store.upsert(
                AccountDraft(
                    email="database@example.com",
                    password="password",
                    access_token="database-access",
                    auth_file=str(legacy_auth / "xai-database@example.com.json"),
                )
            )
            manager = GrokManager(
                config_store=ConfigStore(data_root / "manager-config.json"),
                store=store,
                reference=reference,
            )

            migrated_config = manager.reference.load_registration_config()
            records = manager.reference.import_records()
            stored_after_migration = manager.store.get(stored_before_migration.id)
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
            expected_auth_file = (
                data_root
                / "registration-output"
                / "legacy-import"
                / "out_old"
                / "cpa_auths"
                / "xai-database@example.com.json"
            )
            self.assertEqual(
                expected_auth_file.resolve(),
                Path(stored_after_migration.auth_file)
                if stored_after_migration
                else Path(),
            )

            (legacy_root / "config.json").write_text(
                '{"proxy": "changed"}\n', encoding="utf-8"
            )
            manager.reference.migrate_legacy_data()
            self.assertEqual(
                "http://127.0.0.1:7890",
                manager.reference.load_registration_config()["proxy"],
            )

    def test_missing_legacy_source_does_not_complete_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager_config = root / "legacy-manager-config.json"
            manager_config.write_text(
                json.dumps({"reference_project": "missing-register-project"}),
                encoding="utf-8",
            )
            migration_file = root / "data" / ".migration.json"
            reference = ReferenceProject(
                root=root / "embedded",
                config_file=root / "data" / "registration-config.json",
                config_example_file=root / "example.json",
                output_dir=root / "data" / "registration-output",
                data_root=root / "data",
                legacy_manager_config_file=manager_config,
                migration_file=migration_file,
            )

            reference.migrate_legacy_data()

            self.assertFalse(migration_file.exists())

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

    def test_startup_encrypts_plaintext_registration_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_file = root / "registration-config.json"
            config_file.write_text(
                json.dumps(
                    {
                        "proxy": "http://user:pass@example.test:8080",
                        "cloudflare_api_key": "cloudflare-secret",
                        "email_provider": "cloudflare",
                    }
                ),
                encoding="utf-8",
            )
            manager = make_manager(root)
            on_disk = json.loads(config_file.read_text(encoding="utf-8"))
            loaded = manager.reference.load_registration_config()

            self.assertTrue(
                CredentialVault.is_encrypted(str(on_disk.get("proxy") or ""))
            )
            self.assertTrue(
                CredentialVault.is_encrypted(
                    str(on_disk.get("cloudflare_api_key") or "")
                )
            )
            self.assertNotIn("cloudflare-secret", config_file.read_text(encoding="utf-8"))
            self.assertEqual(
                "http://user:pass@example.test:8080", loaded["proxy"]
            )
            self.assertEqual("cloudflare-secret", loaded["cloudflare_api_key"])

    def test_partial_config_save_encrypts_retained_plaintext_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            manager.reference.config_file.write_text(
                json.dumps(
                    {
                        "proxy": "http://user:pass@example.test:8080",
                        "cloudflare_api_key": "cloudflare-secret",
                        "email_provider": "cloudflare",
                    }
                ),
                encoding="utf-8",
            )
            manager.reference.save_registration_config(
                {"email_provider": "duckmail"}
            )
            on_disk = json.loads(
                manager.reference.config_file.read_text(encoding="utf-8")
            )
            loaded = manager.reference.load_registration_config()

            self.assertTrue(
                CredentialVault.is_encrypted(str(on_disk.get("proxy") or ""))
            )
            self.assertTrue(
                CredentialVault.is_encrypted(
                    str(on_disk.get("cloudflare_api_key") or "")
                )
            )
            self.assertEqual("duckmail", loaded["email_provider"])
            self.assertEqual(
                "http://user:pass@example.test:8080", loaded["proxy"]
            )

    def test_import_does_not_delete_unrelated_auth_or_mail_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            output = manager.reference.output_dir / "out_batch"
            output.mkdir(parents=True)
            accounts_file = output / "accounts.txt"
            accounts_file.write_text(
                "first@example.com----password----first-sso\n",
                encoding="utf-8",
            )
            first_auth = output / "xai-first@example.com.json"
            first_auth.write_text(
                json.dumps(
                    {
                        "email": "first@example.com",
                        "access_token": "first-access",
                        "refresh_token": "first-refresh",
                    }
                ),
                encoding="utf-8",
            )
            other_auth = output / "xai-other@example.com.json"
            other_auth.write_text(
                json.dumps(
                    {
                        "email": "other@example.com",
                        "access_token": "other-access",
                        "refresh_token": "other-refresh",
                    }
                ),
                encoding="utf-8",
            )
            mail_file = output / "mail_credentials.txt"
            mail_file.write_text(
                "first@example.com\tfirst-mail\n"
                "other@example.com\tother-mail\n",
                encoding="utf-8",
            )

            imported = manager.import_reference_accounts([accounts_file])
            remaining_mail = mail_file.read_text(encoding="utf-8")

            self.assertEqual(["first@example.com"], [item.email for item in imported])
            self.assertFalse(first_auth.exists())
            self.assertTrue(other_auth.exists())
            self.assertNotIn("first@example.com", remaining_mail)
            self.assertIn("other@example.com\tother-mail", remaining_mail)
            self.assertEqual(
                "first-mail",
                manager.reference.find_mail_credential("first@example.com"),
            )

    def test_reimport_prefers_new_mail_credential_over_vault(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            output = manager.reference.output_dir / "batch-old"
            output.mkdir(parents=True)
            accounts_file = output / "accounts.txt"
            accounts_file.write_text(
                "first@example.com----password----sso-one\n",
                encoding="utf-8",
            )
            (output / "mail_credentials.txt").write_text(
                "first@example.com\told-mail\n",
                encoding="utf-8",
            )
            manager.import_reference_accounts([accounts_file])
            self.assertEqual(
                "old-mail",
                manager.reference.find_mail_credential("first@example.com"),
            )

            newer = manager.reference.output_dir / "batch-new"
            newer.mkdir(parents=True)
            newer_accounts = newer / "accounts.txt"
            newer_accounts.write_text(
                "first@example.com----password----sso-two\n",
                encoding="utf-8",
            )
            (newer / "mail_credentials.txt").write_text(
                "first@example.com\tnew-mail\n",
                encoding="utf-8",
            )
            manager.import_reference_accounts([newer_accounts])

            self.assertEqual(
                "new-mail",
                manager.reference.find_mail_credential("first@example.com"),
            )
            self.assertFalse((newer / "mail_credentials.txt").exists())

    def test_import_cleans_batch_sub2api_exports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            batch = manager.reference.output_dir / "batch-1"
            cpa_dir = batch / "cpa_auths"
            export_dir = batch / "sub2api_exports"
            cpa_dir.mkdir(parents=True)
            export_dir.mkdir(parents=True)
            accounts_file = batch / "accounts.txt"
            accounts_file.write_text(
                "first@example.com----password----sso-one\n",
                encoding="utf-8",
            )
            auth_file = cpa_dir / "xai-first@example.com.json"
            auth_file.write_text(
                json.dumps(
                    {
                        "email": "first@example.com",
                        "access_token": "access",
                        "refresh_token": "refresh",
                    }
                ),
                encoding="utf-8",
            )
            export_file = export_dir / "sub2api-xai-first@example.com.json"
            export_file.write_text(
                json.dumps(
                    {
                        "accounts": [
                            {
                                "credentials": {
                                    "access_token": "access",
                                    "refresh_token": "refresh",
                                }
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            imported = manager.import_reference_accounts(
                [accounts_file], extra_auth_dirs=[cpa_dir]
            )

            self.assertEqual(["first@example.com"], [item.email for item in imported])
            self.assertFalse(auth_file.exists())
            self.assertFalse(export_dir.exists())


class Grok2ApiRemoteSyncTests(unittest.TestCase):
    @staticmethod
    def _settings() -> dict:
        return {
            "grok2api_remote_base": "https://grok2api.example/admin/api",
            "grok2api_remote_app_key": "test-key",
            "grok2api_pool_name": "ssoBasic",
        }

    def test_relogin_uses_tokens_edit_endpoint(self) -> None:
        from grok_register.app import add_token_to_grok2api_remote_pool

        calls = []

        class Response:
            status_code = 200

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"status": "success", "token": "fresh-sso", "pool": "basic"}

        def put(url, **kwargs):
            calls.append(("PUT", url, kwargs.get("json")))
            return Response()

        with patch("grok_register.app.http_put", side_effect=put), patch(
            "grok_register.app.http_get"
        ) as get, patch("grok_register.app.http_post") as post:
            add_token_to_grok2api_remote_pool(
                "fresh-sso",
                email="same@example.com",
                settings=self._settings(),
                replace_email=True,
                previous_token="old-sso",
            )

        self.assertEqual(
            [
                (
                    "PUT",
                    "https://grok2api.example/admin/api/tokens/edit",
                    {
                        "old_token": "old-sso",
                        "token": "fresh-sso",
                        "pool": "basic",
                    },
                )
            ],
            calls,
        )
        get.assert_not_called()
        post.assert_not_called()

    def test_relogin_refuses_full_pool_rewrite_when_edit_route_unavailable(self) -> None:
        from grok_register.app import add_token_to_grok2api_remote_pool

        class MissingRoute:
            status_code = 405
            text = "Method Not Allowed"

            def raise_for_status(self):
                raise RuntimeError("method not allowed")

        with patch("grok_register.app.http_put", return_value=MissingRoute()), patch(
            "grok_register.app.http_get"
        ) as get, patch("grok_register.app.http_post") as post:
            with self.assertRaisesRegex(RuntimeError, "远端替换失败"):
                add_token_to_grok2api_remote_pool(
                    "fresh-sso",
                    email="same@example.com",
                    settings=self._settings(),
                    replace_email=True,
                    previous_token="old-sso",
                )

        get.assert_not_called()
        post.assert_not_called()

    def test_relogin_refuses_to_add_when_previous_credential_is_missing(self) -> None:
        from grok_register.app import add_token_to_grok2api_remote_pool

        class MissingEdit:
            status_code = 404
            text = '{"detail":"Account not found","code":"account_not_found"}'

            def raise_for_status(self):
                raise RuntimeError("not found")

        class EmptyTokens:
            status_code = 200

            @staticmethod
            def json():
                return {"tokens": []}

        with patch(
            "grok_register.app.http_put", return_value=MissingEdit()
        ), patch(
            "grok_register.app.http_get", return_value=EmptyTokens()
        ) as get, patch("grok_register.app.http_post") as post:
            with self.assertRaisesRegex(RuntimeError, "未找到待替换凭据，已拒绝新增"):
                add_token_to_grok2api_remote_pool(
                    "fresh-sso",
                    email="same@example.com",
                    settings=self._settings(),
                    replace_email=True,
                    previous_token="old-sso",
                )

        get.assert_called()
        post.assert_not_called()

    def test_relogin_generic_not_found_does_not_count_as_missing_account(self) -> None:
        from grok_register.app import add_token_to_grok2api_remote_pool

        class GenericNotFound:
            status_code = 404
            text = '{"detail":"Not Found"}'

            def raise_for_status(self):
                raise RuntimeError("not found")

        with patch(
            "grok_register.app.http_put", return_value=GenericNotFound()
        ), patch("grok_register.app.http_post") as post:
            with self.assertRaisesRegex(RuntimeError, "远端替换失败"):
                add_token_to_grok2api_remote_pool(
                    "fresh-sso",
                    email="same@example.com",
                    settings=self._settings(),
                    replace_email=True,
                    previous_token="old-sso",
                )

        post.assert_not_called()

    def test_registration_still_uses_remote_add_endpoint(self) -> None:
        from grok_register.app import add_token_to_grok2api_remote_pool

        class Response:
            @staticmethod
            def raise_for_status():
                return None

        calls = []

        def post(url, **kwargs):
            calls.append((url, kwargs["json"]))
            return Response()

        with patch("grok_register.app.http_post", side_effect=post), patch(
            "grok_register.app.http_get"
        ) as get:
            add_token_to_grok2api_remote_pool(
                "fresh-sso",
                email="new@example.com",
                settings=self._settings(),
                replace_email=False,
            )

        self.assertEqual(
            [
                (
                    "https://grok2api.example/admin/api/tokens/add",
                    {
                        "tokens": ["fresh-sso"],
                        "pool": "basic",
                        "tags": ["auto-register"],
                    },
                )
            ],
            calls,
        )
        get.assert_not_called()

    def test_relogin_updates_local_pool_even_when_remote_update_fails(self) -> None:
        from grok_register.app import add_token_to_grok2api_pools

        settings = {
            **self._settings(),
            "grok2api_auto_add_remote": True,
            "grok2api_auto_add_local": True,
        }
        events = []
        local_kwargs = {}

        def remote(*_args, **_kwargs):
            events.append("remote")
            raise RuntimeError("remote unavailable")

        def local(*_args, **kwargs):
            events.append("local")
            local_kwargs.update(kwargs)

        with patch(
            "grok_register.app.add_token_to_grok2api_remote_pool",
            side_effect=remote,
        ), patch(
            "grok_register.app.add_token_to_grok2api_local_pool",
            side_effect=local,
        ):
            with self.assertRaisesRegex(RuntimeError, "remote unavailable"):
                add_token_to_grok2api_pools(
                    "fresh-sso",
                    email="same@example.com",
                    settings=settings,
                    replace_email=True,
                    previous_token="old-sso",
                )

        self.assertEqual(["local", "remote"], events)
        self.assertEqual("old-sso", local_kwargs.get("previous_token"))
        self.assertTrue(local_kwargs.get("replace_email"))

    def test_local_pool_replaces_unlabelled_token_by_previous_token(self) -> None:
        from grok_register.app import add_token_to_grok2api_local_pool

        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "tokens.json"
            token_file.write_text(
                json.dumps(
                    {
                        "ssoBasic": [
                            {"token": "old-sso", "tags": ["manual"]},
                            {
                                "token": "other-sso",
                                "tags": ["manual"],
                                "note": "other@example.com",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            settings = {
                "grok2api_local_token_file": str(token_file),
                "grok2api_pool_name": "ssoBasic",
            }
            add_token_to_grok2api_local_pool(
                "fresh-sso",
                email="same@example.com",
                settings=settings,
                replace_email=True,
                previous_token="old-sso",
            )
            pool = json.loads(token_file.read_text(encoding="utf-8"))["ssoBasic"]
            self.assertEqual(
                [
                    {
                        "token": "other-sso",
                        "tags": ["manual"],
                        "note": "other@example.com",
                    },
                    {
                        "token": "fresh-sso",
                        "tags": ["manual"],
                        "note": "same@example.com",
                    },
                ],
                pool,
            )

    def test_relogin_rejects_base_with_embedded_app_key(self) -> None:
        from grok_register.app import add_token_to_grok2api_remote_pool

        with self.assertRaisesRegex(RuntimeError, "不能包含 query"):
            add_token_to_grok2api_remote_pool(
                "fresh-sso",
                email="same@example.com",
                settings={
                    "grok2api_remote_base": "https://host/admin/api?app_key=SECRET",
                    "grok2api_remote_app_key": "SECRET",
                    "grok2api_pool_name": "ssoBasic",
                },
                replace_email=True,
                previous_token="old-sso",
            )

    def test_relogin_token_unchanged_requires_remote_presence(self) -> None:
        from grok_register.app import add_token_to_grok2api_remote_pool

        class EmptyTokens:
            status_code = 200

            @staticmethod
            def json():
                return {"tokens": []}

        with patch("grok_register.app.http_get", return_value=EmptyTokens()), patch(
            "grok_register.app.http_put"
        ) as put, patch("grok_register.app.http_post") as post:
            with self.assertRaisesRegex(RuntimeError, "未找到待同步 token"):
                add_token_to_grok2api_remote_pool(
                    "same-sso",
                    email="same@example.com",
                    settings=self._settings(),
                    replace_email=True,
                    previous_token="same-sso",
                )
        put.assert_not_called()
        post.assert_not_called()

    def test_relogin_account_not_found_is_idempotent_when_new_token_exists(self) -> None:
        from grok_register.app import add_token_to_grok2api_remote_pool

        class MissingOld:
            status_code = 404
            text = '{"detail":"Account not found","code":"account_not_found"}'

            def raise_for_status(self):
                raise RuntimeError("not found")

        class PresentNew:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "tokens": [
                        {"token": "fresh-sso", "pool": "basic", "tags": ["auto-relogin"]}
                    ]
                }

        with patch(
            "grok_register.app.http_put", return_value=MissingOld()
        ), patch(
            "grok_register.app.http_get", return_value=PresentNew()
        ), patch("grok_register.app.http_post") as post:
            ok = add_token_to_grok2api_remote_pool(
                "fresh-sso",
                email="same@example.com",
                settings=self._settings(),
                replace_email=True,
                previous_token="old-sso",
            )
        self.assertTrue(ok)
        post.assert_not_called()

    def test_relogin_raises_when_remote_enabled_without_base_or_app_key(self) -> None:
        from grok_register.app import add_token_to_grok2api_remote_pool

        with self.assertRaisesRegex(RuntimeError, "未配置 base/app_key"):
            add_token_to_grok2api_remote_pool(
                "fresh-sso",
                email="same@example.com",
                settings={
                    "grok2api_remote_base": "",
                    "grok2api_remote_app_key": "",
                    "grok2api_pool_name": "ssoBasic",
                },
                replace_email=True,
            )

    def test_registration_local_pool_failure_does_not_raise(self) -> None:
        from grok_register.app import add_token_to_grok2api_pools

        settings = {
            **self._settings(),
            "grok2api_auto_add_remote": False,
            "grok2api_auto_add_local": True,
        }

        with patch(
            "grok_register.app.add_token_to_grok2api_local_pool",
            side_effect=OSError("disk full"),
        ), patch(
            "grok_register.app.add_token_to_grok2api_remote_pool"
        ) as remote:
            add_token_to_grok2api_pools(
                "fresh-sso",
                email="new@example.com",
                settings=settings,
                replace_email=False,
            )

        remote.assert_not_called()

    def test_relogin_propagates_false_remote_replace_result(self) -> None:
        from grok_register.app import add_token_to_grok2api_pools

        settings = {
            **self._settings(),
            "grok2api_auto_add_remote": True,
            "grok2api_auto_add_local": False,
        }

        with patch(
            "grok_register.app.add_token_to_grok2api_remote_pool",
            return_value=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "远端替换未完成"):
                add_token_to_grok2api_pools(
                    "fresh-sso",
                    email="same@example.com",
                    settings=settings,
                    replace_email=True,
                    previous_token="old-sso",
                )


if __name__ == "__main__":
    unittest.main()
