import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from grok_manager.models import AccountDraft, AccountStatus, InspectionResult
from tests.support import make_manager


def _future_iso(seconds: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=seconds)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _past_iso(seconds: int) -> str:
    return _future_iso(-seconds)


class CpaHotloadSyncTests(unittest.TestCase):
    def _enable_hotload(self, manager, hotload_dir: Path) -> None:
        hotload_dir.mkdir(parents=True, exist_ok=True)
        manager.reference.config_file.write_text(
            json.dumps(
                {
                    "cpa_copy_to_hotload": True,
                    "cpa_hotload_dir": str(hotload_dir),
                    "cpa_base_url": "http://127.0.0.1:8317/v1",
                }
            ),
            encoding="utf-8",
        )

    def _write_hotload(
        self,
        hotload_dir: Path,
        email: str,
        *,
        access: str,
        refresh: str,
        expired: str,
    ) -> Path:
        path = hotload_dir / ("xai-%s.json" % email)
        path.write_text(
            json.dumps(
                {
                    "email": email,
                    "access_token": access,
                    "refresh_token": refresh,
                    "expired": expired,
                    "base_url": "http://127.0.0.1:8317/v1",
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_pulls_newer_hotload_into_manager(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            hotload_dir = root / "hotload"
            self._enable_hotload(manager, hotload_dir)
            account = manager.store.upsert(
                AccountDraft(
                    email="sync@example.com",
                    access_token="old-access",
                    refresh_token="old-refresh",
                    token_expires_at=_past_iso(3600),
                )
            )
            manager.store.apply_inspection(
                InspectionResult(
                    account_id=account.id,
                    status=AccountStatus.EXPIRED.value,
                    detail="old",
                    checked_at=_future_iso(0),
                    expires_at=_past_iso(3600),
                    sso_status=AccountStatus.ACTIVE.value,
                    cpa_status=AccountStatus.EXPIRED.value,
                    cpa_detail="old",
                )
            )
            self._write_hotload(
                hotload_dir,
                account.email,
                access="hot-access",
                refresh="hot-refresh",
                expired=_future_iso(7200),
            )

            result = manager.sync_account_cpa_with_hotload(manager.store.get(account.id))
            stored = manager.store.get(account.id)
            managed = list(manager.reference.managed_auth_dir.glob("xai-*.json"))

            self.assertTrue(result.ok)
            self.assertEqual("pull", result.action)
            self.assertEqual("hot-access", stored.access_token if stored else "")
            self.assertEqual("hot-refresh", stored.refresh_token if stored else "")
            self.assertEqual(1, len(managed))
            payload = json.loads(managed[0].read_text(encoding="utf-8"))
            self.assertEqual("hot-access", payload["access_token"])

    def test_pushes_newer_manager_over_stale_hotload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            hotload_dir = root / "hotload"
            self._enable_hotload(manager, hotload_dir)
            account = manager.store.upsert(
                AccountDraft(
                    email="push@example.com",
                    access_token="mgr-access",
                    refresh_token="mgr-refresh",
                    token_expires_at=_future_iso(8000),
                )
            )
            self._write_hotload(
                hotload_dir,
                account.email,
                access="stale-access",
                refresh="stale-refresh",
                expired=_past_iso(100),
            )

            result = manager.sync_account_cpa_with_hotload(manager.store.get(account.id))
            hot = json.loads(
                (hotload_dir / "xai-push@example.com.json").read_text(encoding="utf-8")
            )

            self.assertTrue(result.ok)
            self.assertEqual("push", result.action)
            self.assertEqual("mgr-access", hot["access_token"])
            self.assertEqual("mgr-refresh", hot["refresh_token"])

    def test_noop_when_tokens_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            hotload_dir = root / "hotload"
            self._enable_hotload(manager, hotload_dir)
            account = manager.store.upsert(
                AccountDraft(
                    email="same@example.com",
                    access_token="same-access",
                    refresh_token="same-refresh",
                    token_expires_at=_future_iso(5000),
                )
            )
            self._write_hotload(
                hotload_dir,
                account.email,
                access="same-access",
                refresh="same-refresh",
                expired=_future_iso(5000),
            )
            result = manager.sync_account_cpa_with_hotload(manager.store.get(account.id))
            self.assertTrue(result.ok)
            self.assertEqual("noop", result.action)

    def test_guard_pulls_hotload_before_marking_expired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            hotload_dir = root / "hotload"
            self._enable_hotload(manager, hotload_dir)
            account = manager.store.upsert(
                AccountDraft(
                    email="guard-pull@example.com",
                    access_token="dead-access",
                    refresh_token="revoked-refresh",
                    token_expires_at=_past_iso(100),
                )
            )
            manager.store.apply_inspection(
                InspectionResult(
                    account_id=account.id,
                    status=AccountStatus.ACTIVE.value,
                    detail="still marked active",
                    checked_at=_future_iso(0),
                    expires_at=_past_iso(100),
                    sso_status=AccountStatus.ACTIVE.value,
                    cpa_status=AccountStatus.ACTIVE.value,
                    cpa_detail="active",
                )
            )
            self._write_hotload(
                hotload_dir,
                account.email,
                access="fresh-hot-access",
                refresh="fresh-hot-refresh",
                expired=_future_iso(9000),
            )

            with patch.object(manager, "inspect_accounts", return_value=[]):
                results = manager.guard_cpa_tokens(lead_seconds=1800)

            stored = manager.store.get(account.id)
            self.assertEqual("fresh-hot-access", stored.access_token if stored else "")
            self.assertEqual("fresh-hot-refresh", stored.refresh_token if stored else "")
            # After hotload pull, account no longer needs refresh; guard should not
            # mark it expired via revoked refresh.
            self.assertTrue(all(item.ok for item in results if item.account_id == account.id))


if __name__ == "__main__":
    unittest.main()
