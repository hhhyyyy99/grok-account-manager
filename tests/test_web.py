import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grok_manager.models import CpaRefreshResult, LoginResult
from grok_manager.web import (
    ASSET_DIR,
    GrokWebApplication,
    TaskRegistry,
    host_header_hostname,
    is_loopback_host,
    is_wildcard_host,
    normalize_bind_host,
    summarize_account_results,
    task_result_failed,
    task_result_outcome,
)
from tests.support import make_manager


class TaskResultStateTests(unittest.TestCase):
    def test_task_result_failed_detects_partial_failures(self) -> None:
        self.assertFalse(task_result_failed({"message": "ok", "failed": 0}))
        self.assertTrue(task_result_failed({"message": "partial", "failed": 1}))
        self.assertTrue(
            task_result_failed(
                {
                    "resetCount": 2,
                    "resetSucceeded": 1,
                    "loginCount": 1,
                    "loginSucceeded": 1,
                }
            )
        )
        self.assertTrue(
            task_result_failed(
                {
                    "resetCount": 1,
                    "resetSucceeded": 1,
                    "loginCount": 1,
                    "loginSucceeded": 0,
                }
            )
        )

    def test_task_result_outcome_distinguishes_partial_from_total_failure(self) -> None:
        self.assertEqual("succeeded", task_result_outcome({"succeeded": 3, "failed": 0}))
        self.assertEqual("partial", task_result_outcome({"succeeded": 2, "failed": 1}))
        self.assertEqual("failed", task_result_outcome({"succeeded": 0, "failed": 4}))
        self.assertEqual(
            "partial",
            task_result_outcome(
                {
                    "resetCount": 2,
                    "resetSucceeded": 2,
                    "loginCount": 2,
                    "loginSucceeded": 1,
                }
            ),
        )
        self.assertEqual(
            "failed",
            task_result_outcome(
                {
                    "resetCount": 1,
                    "resetSucceeded": 0,
                    "loginCount": 0,
                    "loginSucceeded": 0,
                }
            ),
        )

    def test_summarize_account_results_lists_failed_emails(self) -> None:
        summary = summarize_account_results(
            [
                CpaRefreshResult(1, "ok@example.com", True, "done"),
                CpaRefreshResult(2, "bad@example.com", False, "revoked"),
                LoginResult(3, "also-bad@example.com", False, "timeout"),
            ],
            action_label="CPA 续期",
        )
        self.assertEqual(1, summary["succeeded"])
        self.assertEqual(2, summary["failed"])
        self.assertEqual(
            ["bad@example.com", "also-bad@example.com"],
            [item["email"] for item in summary["failures"]],
        )
        self.assertIn("失败账号：bad@example.com", summary["message"])

    def test_registry_marks_partial_login_failure_as_partial(self) -> None:
        registry = TaskRegistry()

        def worker(_task):
            return {
                "count": 2,
                "succeeded": 1,
                "failed": 1,
                "message": "登录完成，成功 1，失败 1",
            }

        task = registry.start("login", "批量登录", worker)
        for _ in range(50):
            if task.state in {"succeeded", "partial", "failed", "cancelled"}:
                break
            import time

            time.sleep(0.01)

        self.assertEqual("partial", task.state)
        self.assertIn("失败 1", task.message)

    def test_registry_marks_total_login_failure_as_failed(self) -> None:
        registry = TaskRegistry()

        def worker(_task):
            return {
                "count": 1,
                "succeeded": 0,
                "failed": 1,
                "message": "登录完成，成功 0，失败 1",
            }

        task = registry.start("login", "批量登录", worker)
        for _ in range(50):
            if task.state in {"succeeded", "partial", "failed", "cancelled"}:
                break
            import time

            time.sleep(0.01)

        self.assertEqual("failed", task.state)
        self.assertIn("失败 1", task.message)


class LanAccessTests(unittest.TestCase):
    def test_host_helpers(self) -> None:
        self.assertEqual("0.0.0.0", normalize_bind_host("0.0.0.0"))
        self.assertEqual("::", normalize_bind_host("[::]"))
        self.assertTrue(is_loopback_host("127.0.0.1"))
        self.assertTrue(is_loopback_host("::1"))
        self.assertFalse(is_loopback_host("0.0.0.0"))
        self.assertTrue(is_wildcard_host("0.0.0.0"))
        self.assertEqual("192.168.1.10", host_header_hostname("192.168.1.10:8787"))
        self.assertEqual("::1", host_header_hostname("[::1]:8787"))
        self.assertIsNone(host_header_hostname(""))

    def test_serve_rejects_non_loopback_without_lan_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            application = GrokWebApplication(make_manager(Path(directory)))
            with self.assertRaisesRegex(ValueError, "--lan"):
                application.serve(host="0.0.0.0", port=0, open_browser=False, allow_lan=False)

    def test_web_app_starts_cpa_guard_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            manager.config.cpa_guard_enabled = True
            application = GrokWebApplication(manager)
            started = {"count": 0}

            def fake_loop(**_kwargs):
                started["count"] += 1
                while not _kwargs["cancelled"]():
                    import time

                    time.sleep(0.01)

            with patch.object(manager, "run_cpa_guard_loop", side_effect=fake_loop):
                self.assertTrue(application.start_cpa_guard())
                self.assertFalse(application.start_cpa_guard())  # already running
                application.stop_cpa_guard(timeout=1.0)
            self.assertEqual(1, started["count"])

    def test_web_app_skips_cpa_guard_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            manager.config.cpa_guard_enabled = False
            application = GrokWebApplication(manager)
            self.assertFalse(application.start_cpa_guard())
            self.assertTrue(application.start_cpa_guard(force=True))
            application.stop_cpa_guard(timeout=1.0)


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


    def test_password_reset_action_is_exposed(self) -> None:
        html = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
        script = (ASSET_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="reset-password-selected"', html)
        self.assertIn('/api/reset-password', script)
        self.assertIn('reset-password', script)

    def test_cpa_refresh_action_is_exposed(self) -> None:
        html = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
        script = (ASSET_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="refresh-cpa-selected"', html)
        self.assertIn('/api/refresh-cpa', script)

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


    def test_registration_config_masks_secrets_and_merges_partial_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            manager.reference.save_registration_config(
                {
                    "proxy": "http://user:pass@example.test:8080",
                    "cloudflare_api_key": "cloudflare-secret",
                    "email_provider": "cloudflare",
                    "cpa_base_url": "https://cpa.example.test/v1",
                }
            )
            application = GrokWebApplication(manager)
            response = application.config_json()
            self.assertEqual("", response["registration"]["proxy"])
            self.assertEqual("", response["registration"]["cloudflare_api_key"])
            self.assertTrue(response["registrationSecrets"]["proxy"])
            self.assertTrue(response["registrationSecrets"]["cloudflare_api_key"])

            application.save_reference_config(
                {
                    "email_provider": "duckmail",
                    "cpa_base_url": "https://cpa.example.test/v2",
                }
            )
            loaded = manager.reference.load_registration_config()
            self.assertEqual("http://user:pass@example.test:8080", loaded["proxy"])
            self.assertEqual("cloudflare-secret", loaded["cloudflare_api_key"])
            self.assertEqual("duckmail", loaded["email_provider"])
            self.assertEqual("https://cpa.example.test/v2", loaded["cpa_base_url"])

            application.save_reference_config({"proxy": None})
            self.assertEqual("", manager.reference.load_registration_config()["proxy"])

if __name__ == "__main__":
    unittest.main()
