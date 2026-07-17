import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grok_manager.web import ASSET_DIR, GrokWebApplication
from tests.support import make_manager


class ManagerTaskConfigTests(unittest.TestCase):
    def test_status_filter_includes_checking(self) -> None:
        html = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn('<option value="checking">巡检中</option>', html)

    def test_task_config_controls_registration_and_relogin_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            application = GrokWebApplication(manager)

            response = application.save_manager_config(
                {
                    "register_count": 40,
                    "register_threads": 3,
                    "mint_workers": 2,
                    "login_workers": 4,
                    "login_timeout_seconds": 600,
                }
            )

            self.assertEqual(
                {
                    "register_count": 40,
                    "register_threads": 3,
                    "mint_workers": 2,
                    "login_workers": 4,
                    "login_timeout_seconds": 600,
                },
                {
                    key: response["manager"][key]
                    for key in (
                        "register_count",
                        "register_threads",
                        "mint_workers",
                        "login_workers",
                        "login_timeout_seconds",
                    )
                },
            )

            with patch("grok_manager.web.RegistrationRequest") as request_type:
                with patch.object(application.tasks, "start", return_value=object()):
                    application.start_registration({})

            request_type.assert_called_once_with(count=40, threads=3, mint_workers=2)


if __name__ == "__main__":
    unittest.main()
