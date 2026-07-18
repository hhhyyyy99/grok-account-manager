import json
import shutil
import subprocess
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
        self.assertIn('<option value="missing_cpa">缺少 CPA 凭据</option>', html)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for asset tests")
    def test_account_order_ignores_status_changes(self) -> None:
        accounts = [
            {"id": 3, "email": "c", "status": "unknown"},
            {"id": 2, "email": "d", "status": "unknown"},
            {"id": 1, "email": "e", "status": "unknown"},
            {"id": 4, "email": "b", "status": "checking"},
            {"id": 5, "email": "a", "status": "checking"},
        ]
        source = """
const { orderAccountsById } = require(process.argv[1]);
const accounts = JSON.parse(process.argv[2]);
process.stdout.write(JSON.stringify(orderAccountsById(accounts).map((item) => item.email)));
"""

        completed = subprocess.run(
            [
                "node",
                "-e",
                source,
                str(ASSET_DIR / "app.js"),
                json.dumps(accounts),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual(["a", "b", "c", "d", "e"], json.loads(completed.stdout))


    def test_registration_config_is_split_by_integration(self) -> None:
        html = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
        script = (ASSET_DIR / "app.js").read_text(encoding="utf-8")
        sections = {
            "registration": "registration-base-config-form",
            "email": "email-config-form",
            "cpa": "cpa-config-form",
            "sub2api": "sub2api-config-form",
            "grok2api": "grok2api-config-form",
        }

        self.assertNotIn(">注册环境</button>", html)
        for target, form_id in sections.items():
            self.assertIn(f'data-settings-target="{target}"', html)
            self.assertIn(f'data-settings-view="{target}"', html)
            self.assertIn(f'id="{form_id}"', html)
            self.assertIn(f'"{form_id}"', script)

        def form_markup(form_id: str) -> str:
            start = html.index(f'id="{form_id}"')
            return html[start:html.index("</form>", start)]

        email_form = form_markup("email-config-form")
        cpa_form = form_markup("cpa-config-form")
        sub2api_form = form_markup("sub2api-config-form")
        grok2api_form = form_markup("grok2api-config-form")
        self.assertIn('name="email_provider"', email_form)
        self.assertNotIn('name="cpa_base_url"', email_form)
        self.assertIn('name="cpa_base_url"', cpa_form)
        self.assertIn('name="sub2api_export_enabled"', sub2api_form)
        self.assertIn('name="grok2api_auto_add_local"', grok2api_form)

    def test_account_page_exposes_three_export_formats(self) -> None:
        html = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
        script = (ASSET_DIR / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="export-accounts"', html)
        self.assertIn('<option value="cpa">CPA ZIP</option>', html)
        self.assertIn('<option value="sub2api">Sub2API JSON</option>', html)
        self.assertIn('<option value="grok2api">Grok2API JSON</option>', html)
        self.assertIn('fetch("/api/accounts/export"', script)

    def test_account_actions_share_a_persistent_selection_toolbar(self) -> None:
        html = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
        script = (ASSET_DIR / "app.js").read_text(encoding="utf-8")
        selection_bar = html[
            html.index('<div class="selection-bar"'):
            html.index('<div class="table-wrap"')
        ]
        heading_actions = html[
            html.index('<div class="heading-actions">'):
            html.index("</div>", html.index('<div class="heading-actions">'))
        ]

        self.assertNotIn("hidden", selection_bar.split(">", 1)[0])
        self.assertNotIn('id="select-current-page"', selection_bar)
        self.assertIn('id="select-all-results"', selection_bar)
        self.assertIn('id="clear-selection"', selection_bar)
        for control_id in ("inspect-selected", "login-selected", "export-accounts", "delete-selected"):
            self.assertIn(f'id="{control_id}" disabled', selection_bar)
        self.assertIn('id="import-file-button"', heading_actions)
        self.assertIn('id="import-history-button"', heading_actions)
        self.assertNotIn('id="export-accounts"', heading_actions)
        self.assertIn('/api/accounts/selection?', script)

    def test_registration_save_captures_form_before_async_work(self) -> None:
        script = (ASSET_DIR / "app.js").read_text(encoding="utf-8")
        start = script.index("async function saveRegistrationSection")
        end = script.index("async function saveReferenceJson", start)
        save_function = script[start:end]

        capture = save_function.index("const form = event.currentTarget;")
        async_boundary = save_function.index("await loadConfig()")
        self.assertLess(capture, async_boundary)
        self.assertIn("formValues(form)", save_function)
        self.assertIn("form.dataset.configLabel", save_function)
        self.assertNotIn("event.currentTarget.dataset", save_function)

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
