import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
