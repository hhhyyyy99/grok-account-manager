import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from grok_manager.paths import _default_data_dir, _migrate_legacy_install_data


class DataDirectoryTests(unittest.TestCase):
    def test_installed_upgrade_migrates_database_config_and_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "site-packages" / "data"
            source.mkdir(parents=True)
            source_database = source / "accounts.sqlite3"
            connection = sqlite3.connect(source_database)
            try:
                connection.execute("CREATE TABLE accounts (email TEXT NOT NULL)")
                connection.execute(
                    "INSERT INTO accounts (email) VALUES (?)", ("old@example.com",)
                )
                connection.commit()
            finally:
                connection.close()
            (source / "registration-output").mkdir()
            (source / "registration-output" / "accounts.txt").write_text(
                "old@example.com----password----sso\n", encoding="utf-8"
            )
            source_config = root / "site-packages" / "config.json"
            source_config.write_text(
                json.dumps({"inspection_workers": 3}), encoding="utf-8"
            )
            destination = root / "user-data"
            marker = destination / ".migration.json"

            migrated = _migrate_legacy_install_data(
                source, source_config, destination, marker
            )

            migrated_database = sqlite3.connect(destination / "accounts.sqlite3")
            try:
                account_count = migrated_database.execute(
                    "SELECT COUNT(*) FROM accounts"
                ).fetchone()[0]
            finally:
                migrated_database.close()
            self.assertEqual(
                (True, 1, True, {"inspection_workers": 3}, True),
                (
                    migrated,
                    account_count,
                    (destination / "registration-output" / "accounts.txt").is_file(),
                    json.loads(
                        (destination / "manager-config.json").read_text(
                            encoding="utf-8"
                        )
                    ),
                    marker.is_file(),
                ),
            )

    def test_source_checkout_uses_repository_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "grok_manager"
            package.mkdir()
            (root / "grok_register").mkdir()
            (root / "pyproject.toml").write_text("", encoding="utf-8")

            self.assertEqual(
                root / "data",
                _default_data_dir(package, "darwin", {}, root / "home"),
            )

    def test_installed_package_uses_user_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "site-packages" / "grok_manager"
            package.mkdir(parents=True)
            home = root / "home"

            data_dir = _default_data_dir(package, "darwin", {}, home)

            self.assertEqual(
                home / "Library" / "Application Support" / "grok-account-manager",
                data_dir,
            )
            self.assertNotIn(package.parent, data_dir.parents)


if __name__ == "__main__":
    unittest.main()
