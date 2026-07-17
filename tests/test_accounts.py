import sqlite3
import tempfile
import unittest
from pathlib import Path

from grok_manager.models import AccountDraft, AccountStatus, InspectionResult
from grok_manager.store import AccountStore
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
            store = AccountStore(database)
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

            migrated = AccountStore(database).get(account.id)

            self.assertIsNotNone(migrated)
            self.assertEqual(
                (AccountStatus.UNKNOWN.value, ""),
                (
                    migrated.status if migrated else "",
                    migrated.status_detail if migrated else "",
                ),
            )


if __name__ == "__main__":
    unittest.main()
