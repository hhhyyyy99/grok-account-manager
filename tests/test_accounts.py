import sqlite3
import tempfile
import unittest
from pathlib import Path

from grok_manager.models import AccountDraft, AccountStatus, InspectionResult
from grok_manager.store import AccountStore
from grok_manager.vault import CredentialVault, KdfParameters
from grok_manager.web import GrokWebApplication
from tests.support import make_manager


class AccountImportQueryTests(unittest.TestCase):
    def test_manual_import_reports_unique_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))

            imported = manager.import_account_text(
                "ALICE@example.com----old-password----old-sso\n"
                "alice@example.com----new-password"
            )

            self.assertEqual(1, len(imported))

    def test_manual_import_accepts_tab_separated_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))

            imported = manager.import_account_text(
                "账户\t密码\ttoken\n"
                "alice@example.com\tpassword\tfresh-sso\n"
                "bob@example.com\tbob-password\tbob-sso\n"
            )

            self.assertEqual(2, len(imported))
            by_email = {account.email: account for account in imported}
            self.assertEqual("password", by_email["alice@example.com"].password)
            self.assertEqual("fresh-sso", by_email["alice@example.com"].sso_token)
            self.assertEqual("bob-password", by_email["bob@example.com"].password)
            self.assertEqual("bob-sso", by_email["bob@example.com"].sso_token)

    def test_email_search_treats_wildcards_as_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            manager.import_account_text(
                "alice@example.com----alice-password\n"
                "bob@example.com----bob-password"
            )

            matches = manager.store.list_accounts(search="%")

            self.assertEqual([], matches)
            self.assertEqual(0, manager.store.count_accounts(search="%"))

    def test_account_store_paginates_filtered_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            manager.import_account_text(
                "alice@example.com----password\n"
                "bob@example.com----password\n"
                "carol@other.test----password\n"
                "dave@example.com----password"
            )

            all_matches = manager.store.list_accounts(search="example.com")
            page = manager.store.list_accounts(search="example.com", limit=2, offset=1)

            self.assertEqual(3, manager.store.count_accounts(search="example.com"))
            self.assertEqual(
                [account.id for account in all_matches[1:3]],
                [account.id for account in page],
            )

    def test_inspection_does_not_reorder_account_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            manager.import_account_text(
                "alice@example.com----password\n"
                "bob@example.com----password\n"
                "carol@example.com----password"
            )

            before = [account.id for account in manager.store.list_accounts()]
            target_id = before[-1]
            manager.store.apply_inspection(
                InspectionResult(
                    account_id=target_id,
                    status="active",
                    detail="巡检通过",
                    checked_at="2099-01-01T00:00:00Z",
                    sso_status="active",
                    cpa_status="active",
                )
            )
            after = [account.id for account in manager.store.list_accounts()]

            self.assertEqual(before, after)

    def test_web_state_returns_pagination_and_clamps_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            manager.import_account_text(
                "\n".join(
                    "user%s@example.com----password" % index
                    for index in range(1, 6)
                )
            )
            application = GrokWebApplication(manager)

            second_page = application.state_json({"page": ["2"], "page_size": ["2"]})
            last_page = application.state_json({"page": ["99"], "page_size": ["2"]})

            self.assertEqual(
                {"page": 2, "pageSize": 2, "total": 5, "totalPages": 3},
                second_page["pagination"],
            )
            self.assertEqual(2, len(second_page["accounts"]))
            self.assertEqual(3, last_page["pagination"]["page"])
            self.assertEqual(1, len(last_page["accounts"]))

    def test_web_selection_returns_every_id_in_the_current_filter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            manager.import_account_text(
                "alice@example.com----password\n"
                "bob@example.com----password\n"
                "carol@other.test----password\n"
                "dave@example.com----password"
            )
            application = GrokWebApplication(manager)

            selection = application.selection_json({"search": ["example.com"]})
            expected = manager.store.list_accounts(search="example.com")

            self.assertEqual(3, selection["total"])
            self.assertEqual(
                [account.id for account in expected],
                selection["ids"],
            )

    def test_missing_cpa_filter_marks_only_accounts_without_cpa_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            missing = manager.store.upsert(
                AccountDraft(
                    email="missing@example.com",
                    sso_token="valid-sso",
                )
            )
            manager.store.upsert(
                AccountDraft(
                    email="token@example.com",
                    access_token="valid-access",
                )
            )
            manager.store.upsert(
                AccountDraft(
                    email="file@example.com",
                    auth_file="/tmp/xai-file@example.com.json",
                )
            )
            application = GrokWebApplication(manager)

            filtered = manager.store.list_accounts(status="missing_cpa")
            state = application.state_json(
                {"status": ["missing_cpa"], "page": ["1"], "page_size": ["50"]}
            )

            self.assertEqual([missing.id], [account.id for account in filtered])
            self.assertEqual(1, manager.store.count_accounts(status="missing_cpa"))
            self.assertEqual(1, state["pagination"]["total"])
            self.assertEqual(
                ("missing_cpa", "缺少 CPA 凭据", True),
                (
                    state["accounts"][0]["cpaStatus"],
                    state["accounts"][0]["cpaStatusLabel"],
                    state["accounts"][0]["missingCpa"],
                ),
            )

    def test_store_removes_legacy_logging_in_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "accounts.sqlite3"
            vault = CredentialVault(
                database.with_name("credentials.vault.json"),
                kdf_parameters=KdfParameters(memory_cost=8 * 1024, iterations=1, lanes=1),
            )
            vault.initialize("test vault password 123")
            store = AccountStore(database, vault=vault)
            account = store.upsert(
                AccountDraft(email="legacy@example.com", password="password")
            )
            connection = sqlite3.connect(str(database))
            try:
                connection.execute(
                    "UPDATE accounts SET status = ?, status_detail = ? WHERE id = ?",
                    ("logging_in", "旧的登录任务状态", account.id),
                )
                connection.commit()
            finally:
                connection.close()

            migrated = AccountStore(database, vault=vault).get(account.id)

            self.assertIsNotNone(migrated)
            self.assertEqual(
                (AccountStatus.UNKNOWN.value, ""),
                (
                    migrated.status if migrated else "",
                    migrated.status_detail if migrated else "",
                ),
            )
            self.assertTrue(migrated.enabled if migrated else False)

    def test_store_migrates_pre_enabled_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "accounts.sqlite3"
            vault = CredentialVault(
                database.with_name("credentials.vault.json"),
                kdf_parameters=KdfParameters(memory_cost=8 * 1024, iterations=1, lanes=1),
            )
            vault.initialize("test vault password 123")
            connection = sqlite3.connect(str(database))
            try:
                connection.executescript(
                    """
                    CREATE TABLE accounts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                        password TEXT NOT NULL DEFAULT '',
                        sso_token TEXT NOT NULL DEFAULT '',
                        access_token TEXT NOT NULL DEFAULT '',
                        refresh_token TEXT NOT NULL DEFAULT '',
                        token_expires_at TEXT NOT NULL DEFAULT '',
                        auth_file TEXT NOT NULL DEFAULT '',
                        source TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'unknown',
                        status_detail TEXT NOT NULL DEFAULT '',
                        last_checked_at TEXT NOT NULL DEFAULT '',
                        last_login_at TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    INSERT INTO accounts (
                        email, password, created_at, updated_at
                    ) VALUES (
                        'legacy@example.com', 'password',
                        '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z'
                    );
                    """
                )
                connection.commit()
            finally:
                connection.close()

            store = AccountStore(database, vault=vault)
            account = store.get_by_email("legacy@example.com")

            self.assertIsNotNone(account)
            self.assertTrue(account.enabled if account else False)
            self.assertEqual(1, store.stats()["enabled"])
            self.assertEqual(0, store.stats()["disabled"])

    def test_account_enabled_toggle_and_filter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            manager.import_account_text(
                "alice@example.com----password\n"
                "bob@example.com----password\n"
                "carol@example.com----password"
            )
            accounts = manager.store.list_accounts()
            self.assertTrue(all(account.enabled for account in accounts))

            updated = manager.store.set_enabled([accounts[0].id, accounts[1].id], False)
            self.assertEqual(2, updated)
            disabled = manager.store.list_accounts(enabled=False)
            enabled = manager.store.list_accounts(enabled=True)
            self.assertEqual(
                sorted([accounts[0].id, accounts[1].id]),
                sorted([account.id for account in disabled]),
            )
            self.assertEqual([accounts[2].id], [account.id for account in enabled])

            ordered = manager.store.list_accounts()
            self.assertTrue(ordered[0].enabled)
            self.assertFalse(any(account.enabled for account in ordered[1:]))

            application = GrokWebApplication(manager)
            state = application.state_json(
                {"enabled": ["disabled"], "page": ["1"], "page_size": ["50"]}
            )
            stats = manager.store.stats()
            self.assertEqual(2, state["pagination"]["total"])
            self.assertFalse(state["accounts"][0]["enabled"])
            self.assertEqual(
                {"total": 3, "enabled": 1, "disabled": 2},
                {
                    "total": stats["total"],
                    "enabled": stats["enabled"],
                    "disabled": stats["disabled"],
                },
            )

            restored = manager.store.set_enabled([accounts[0].id], True)
            self.assertEqual(1, restored)
            self.assertTrue(manager.store.get(accounts[0].id).enabled)


if __name__ == "__main__":
    unittest.main()
