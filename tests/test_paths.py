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
                connection.execute(
                    """
                    CREATE TABLE accounts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                        password TEXT NOT NULL DEFAULT '',
                        sso_token TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO accounts (email, password, sso_token, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ("old@example.com", "old-password", "old-sso", "2026-01-01", "2026-01-01"),
                )
                connection.execute(
                    """
                    INSERT INTO accounts (email, password, sso_token, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ("shared@example.com", "old-password", "old-sso", "2026-01-01", "2026-01-01"),
                )
                connection.commit()
            finally:
                connection.close()
            (source / "registration-output").mkdir()
            (source / "registration-output" / "accounts.txt").write_text(
                "old@example.com----password----sso\n", encoding="utf-8"
            )
            (source / "registration-config.json").write_text(
                json.dumps(
                    {
                        "proxy": "http://127.0.0.1:7890",
                        "cpa_auth_dir": str(source / "registration-output" / "cpa_auths"),
                    }
                ),
                encoding="utf-8",
            )
            source_config = root / "site-packages" / "config.json"
            source_config.write_text(
                json.dumps({"inspection_workers": 3}), encoding="utf-8"
            )
            destination = root / "user-data"
            destination.mkdir()
            destination_database = sqlite3.connect(destination / "accounts.sqlite3")
            try:
                destination_database.execute(
                    """
                    CREATE TABLE accounts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                        password TEXT NOT NULL DEFAULT '',
                        sso_token TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                destination_database.execute(
                    """
                    INSERT INTO accounts (email, password, sso_token, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ("shared@example.com", "new-password", "", "2026-07-01", "2026-07-01"),
                )
                destination_database.execute(
                    """
                    INSERT INTO accounts (email, password, sso_token, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ("new@example.com", "new-password", "new-sso", "2026-07-01", "2026-07-01"),
                )
                destination_database.commit()
            finally:
                destination_database.close()
            marker = destination / ".migration.json"

            migrated = _migrate_legacy_install_data(
                source, source_config, destination, marker
            )

            migrated_database = sqlite3.connect(destination / "accounts.sqlite3")
            try:
                account_count = migrated_database.execute(
                    "SELECT COUNT(*) FROM accounts"
                ).fetchone()[0]
                shared = migrated_database.execute(
                    "SELECT password, sso_token FROM accounts WHERE email = ?",
                    ("shared@example.com",),
                ).fetchone()
            finally:
                migrated_database.close()
            self.assertEqual(
                (
                    True,
                    3,
                    ("new-password", "old-sso"),
                    True,
                    {"inspection_workers": 3},
                    str(destination / "registration-output" / "cpa_auths"),
                    True,
                ),
                (
                    migrated,
                    account_count,
                    shared,
                    (destination / "registration-output" / "accounts.txt").is_file(),
                    json.loads(
                        (destination / "manager-config.json").read_text(
                            encoding="utf-8"
                        )
                    ),
                    json.loads(
                        (destination / "registration-config.json").read_text(
                            encoding="utf-8"
                        )
                    )["cpa_auth_dir"],
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
