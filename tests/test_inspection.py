import base64
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from grok_manager.inspection import TokenInspector
from grok_manager.models import AccountDraft, AccountStatus, InspectionResult
from grok_manager.web import GrokWebApplication, TERMINAL_STATES
from tests.support import make_manager


def jwt_with_exp(expiration: int) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": expiration}).encode("ascii")
    ).decode("ascii").rstrip("=")
    return "header.%s.signature" % payload


class BlockingInspector:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def inspect(self, account, live=True) -> InspectionResult:
        self.calls.append(account.id)
        self.started.set()
        self.release.wait(5)
        return InspectionResult(
            account_id=account.id,
            status=AccountStatus.ACTIVE.value,
            detail="巡检通过",
            checked_at="2099-01-01T00:00:00Z",
            sso_status=AccountStatus.ACTIVE.value,
            cpa_status=AccountStatus.ACTIVE.value,
        )


class InspectionClassificationTests(unittest.TestCase):
    def test_out_of_range_jwt_expiry_is_unknown_in_local_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(
                    email="large-exp@example.com",
                    password="password",
                    sso_token=jwt_with_exp(10**100),
                )
            )

            result = manager.inspect_accounts([account.id], live=False)[0]

            self.assertEqual(AccountStatus.UNKNOWN.value, result.status)

    def test_expired_cpa_marks_valid_sso_account_expired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(
                    email="expired-cpa@example.com",
                    password="password",
                    sso_token=jwt_with_exp(4102444800),
                    access_token=jwt_with_exp(4102444800),
                    token_expires_at="2000-01-01T00:00:00Z",
                )
            )

            result = manager.inspect_accounts([account.id], live=False)[0]

            self.assertEqual(
                (
                    AccountStatus.EXPIRED.value,
                    AccountStatus.ACTIVE.value,
                    AccountStatus.EXPIRED.value,
                ),
                (result.status, result.sso_status, result.cpa_status),
            )

    def test_loopback_cpa_uses_synced_hotload_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            auth_file = root / "xai-loopback@example.com.json"
            hotload_dir = root / "cpa-hotload"
            hotload_dir.mkdir()
            payload = {
                "email": "loopback@example.com",
                "access_token": "fresh-access",
                "refresh_token": "fresh-refresh",
                "expired": "2099-01-01T00:00:00Z",
                "base_url": "http://127.0.0.1:8317/v1",
            }
            auth_file.write_text(json.dumps(payload), encoding="utf-8")
            (hotload_dir / auth_file.name).write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            account = manager.store.upsert(
                AccountDraft(
                    email="loopback@example.com",
                    password="password",
                    sso_token=jwt_with_exp(4102444800),
                    access_token="fresh-access",
                    refresh_token="fresh-refresh",
                    token_expires_at="2099-01-01T00:00:00Z",
                    auth_file=str(auth_file),
                )
            )

            result = TokenInspector(
                cpa_hotload_dir=str(hotload_dir)
            )._inspect_cpa(account, live=True)

            self.assertEqual(AccountStatus.ACTIVE.value, result.status)
            self.assertIn("hotload 凭据已同步", result.detail)

    def test_credentials_without_sso_need_login(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(email="missing-sso@example.com", password="password")
            )

            result = manager.inspect_accounts([account.id], live=False)[0]

            self.assertEqual(AccountStatus.NEEDS_LOGIN.value, result.status)

    def test_only_accounts_with_an_active_worker_are_checking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            accounts = [
                manager.store.upsert(
                    AccountDraft(email="queued%s@example.com" % index, password="password")
                )
                for index in range(3)
            ]
            inspector = BlockingInspector()
            manager.inspection.inspector = inspector
            manager.inspection.max_workers = 1
            errors = []

            def inspect_all() -> None:
                try:
                    manager.inspect_accounts(
                        [account.id for account in accounts],
                        live=False,
                    )
                except Exception as exc:
                    errors.append(exc)

            worker = threading.Thread(target=inspect_all)
            worker.start()
            try:
                self.assertTrue(inspector.started.wait(1))
                statuses = [manager.store.get(account.id).status for account in accounts]
                self.assertEqual(1, statuses.count(AccountStatus.CHECKING.value))
                self.assertEqual(2, statuses.count(AccountStatus.UNKNOWN.value))
            finally:
                inspector.release.set()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual([], errors)

    def test_cancelling_inspection_skips_queued_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            accounts = [
                manager.store.upsert(
                    AccountDraft(email="cancel%s@example.com" % index, password="password")
                )
                for index in range(3)
            ]
            inspector = BlockingInspector()
            manager.inspection.inspector = inspector
            manager.inspection.max_workers = 1
            application = GrokWebApplication(manager)
            task = application.start_inspection(
                {"ids": [account.id for account in accounts], "live": False}
            )

            cancel_error = None
            try:
                self.assertTrue(inspector.started.wait(1))
                application.cancel_task(task.id)
            except Exception as exc:
                cancel_error = exc
            finally:
                inspector.release.set()
                for _ in range(200):
                    if task.state in TERMINAL_STATES:
                        break
                    time.sleep(0.01)

            if cancel_error is not None:
                raise cancel_error
            self.assertEqual("cancelled", task.state)
            self.assertEqual(1, len(inspector.calls))
            statuses = [manager.store.get(account.id).status for account in accounts]
            self.assertEqual(2, statuses.count(AccountStatus.UNKNOWN.value))


if __name__ == "__main__":
    unittest.main()
