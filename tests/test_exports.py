import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from grok_manager.exports import AccountExporter
from grok_manager.models import AccountDraft
from grok_manager.web import GrokWebApplication
from tests.support import make_manager


class AccountExportTests(unittest.TestCase):
    def test_exports_cpa_sub2api_and_grok2api_formats(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            auth_file = root / "xai-alice@example.com.json"
            auth_file.write_text(
                json.dumps(
                    {
                        "email": "alice@example.com",
                        "access_token": "stale-access",
                        "refresh_token": "stale-refresh",
                        "id_token": "preserved-id-token",
                    }
                ),
                encoding="utf-8",
            )
            exportable = manager.store.upsert(
                AccountDraft(
                    email="alice@example.com",
                    password="password",
                    sso_token="sso=fresh-sso",
                    access_token="fresh-access",
                    refresh_token="fresh-refresh",
                    token_expires_at="2099-01-01T00:00:00Z",
                    auth_file=str(auth_file),
                )
            )
            missing = manager.store.upsert(
                AccountDraft(email="missing@example.com", password="password")
            )
            exporter = AccountExporter(
                {
                    "cpa_base_url": "https://cpa.example.test/v1",
                    "grok2api_pool_name": "ssoSuper",
                }
            )

            cpa_export = exporter.export([exportable, missing], "cpa")
            self.assertEqual("application/zip", cpa_export.content_type)
            self.assertEqual((1, 1), (cpa_export.exported_count, cpa_export.skipped_count))
            with zipfile.ZipFile(io.BytesIO(cpa_export.body)) as archive:
                self.assertEqual(["xai-alice@example.com.json"], archive.namelist())
                cpa = json.loads(archive.read(archive.namelist()[0]))
            self.assertEqual(
                (
                    "fresh-access",
                    "fresh-refresh",
                    "preserved-id-token",
                    "https://cpa.example.test/v1",
                ),
                (
                    cpa["access_token"],
                    cpa["refresh_token"],
                    cpa["id_token"],
                    cpa["base_url"],
                ),
            )

            sub2api_export = exporter.export([exportable, missing], "sub2api")
            sub2api = json.loads(sub2api_export.body)
            self.assertEqual((1, 1), (sub2api_export.exported_count, sub2api_export.skipped_count))
            self.assertEqual("grok", sub2api["accounts"][0]["platform"])
            self.assertEqual(
                "fresh-access",
                sub2api["accounts"][0]["credentials"]["access_token"],
            )

            grok2api_export = exporter.export([exportable, missing], "grok2api")
            grok2api = json.loads(grok2api_export.body)
            self.assertEqual((1, 1), (grok2api_export.exported_count, grok2api_export.skipped_count))
            self.assertEqual(
                [{"token": "fresh-sso", "tags": ["grok-account-manager"], "note": "alice@example.com"}],
                grok2api["ssoSuper"],
            )

    def test_web_export_uses_selected_ids_or_current_filter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            alice = manager.store.upsert(
                AccountDraft(email="alice@example.com", sso_token="alice-sso")
            )
            manager.store.upsert(
                AccountDraft(email="bob@example.com", sso_token="bob-sso")
            )
            application = GrokWebApplication(manager)

            selected = application.export_accounts(
                {"format": "grok2api", "ids": [alice.id]}
            )
            filtered = application.export_accounts(
                {"format": "grok2api", "ids": [], "search": "bob@"}
            )

            self.assertEqual("alice-sso", json.loads(selected.body)["ssoBasic"][0]["token"])
            self.assertEqual("bob-sso", json.loads(filtered.body)["ssoBasic"][0]["token"])

    def test_export_rejects_unknown_format_or_missing_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            unrelated_auth = root / "xai-other@example.com.json"
            unrelated_auth.write_text(
                json.dumps(
                    {"email": "other@example.com", "access_token": "other-secret"}
                ),
                encoding="utf-8",
            )
            account = manager.store.upsert(
                AccountDraft(
                    email="empty@example.com",
                    auth_file=str(unrelated_auth),
                )
            )
            exporter = AccountExporter()

            with self.assertRaisesRegex(ValueError, "导出格式"):
                exporter.export([account], "unknown")
            with self.assertRaisesRegex(ValueError, "没有可导出的 CPA"):
                exporter.export([account], "cpa")


if __name__ == "__main__":
    unittest.main()
