import tempfile
import unittest
from pathlib import Path

from grok_manager.paths import _default_data_dir


class DataDirectoryTests(unittest.TestCase):
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
