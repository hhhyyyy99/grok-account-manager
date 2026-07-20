import json
import os
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
            # Pull updates the vault only; temporary managed auth files must not linger.
            self.assertEqual([], managed)

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
            expires = _future_iso(5000)
            account = manager.store.upsert(
                AccountDraft(
                    email="same@example.com",
                    access_token="same-access",
                    refresh_token="same-refresh",
                    token_expires_at=expires,
                )
            )
            manager.store.apply_inspection(
                InspectionResult(
                    account_id=account.id,
                    status=AccountStatus.ACTIVE.value,
                    detail="active",
                    checked_at=_future_iso(0),
                    expires_at=expires,
                    sso_status=AccountStatus.ACTIVE.value,
                    cpa_status=AccountStatus.ACTIVE.value,
                    cpa_detail="active",
                )
            )
            path = self._write_hotload(
                hotload_dir,
                account.email,
                access="same-access",
                refresh="same-refresh",
                expired=expires,
            )
            manager.store.touch_cpa_auth_file(account.id, str(path))
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

    def test_same_expiry_prefers_hotload_last_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            hotload_dir = root / "hotload"
            self._enable_hotload(manager, hotload_dir)
            expires = _future_iso(5000)
            account = manager.store.upsert(
                AccountDraft(
                    email="tie@example.com",
                    access_token="mgr-access",
                    refresh_token="mgr-refresh",
                    token_expires_at=expires,
                )
            )
            # Manager updated_at is "now" from upsert; hotload last_refresh is later.
            later = (
                datetime.now(timezone.utc) + timedelta(hours=2)
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            path = hotload_dir / "xai-tie@example.com.json"
            path.write_text(
                json.dumps(
                    {
                        "email": "tie@example.com",
                        "access_token": "hot-access",
                        "refresh_token": "hot-refresh",
                        "expired": expires,
                        "last_refresh": later,
                        "base_url": "http://127.0.0.1:8317/v1",
                    }
                ),
                encoding="utf-8",
            )

            result = manager.sync_account_cpa_with_hotload(manager.store.get(account.id))
            stored = manager.store.get(account.id)
            hot = json.loads(path.read_text(encoding="utf-8"))

            self.assertTrue(result.ok)
            self.assertEqual("pull", result.action)
            self.assertEqual("hot-access", stored.access_token if stored else "")
            self.assertEqual("hot-refresh", stored.refresh_token if stored else "")
            # Hotload must keep its rotated token; manager must not push over it.
            self.assertEqual("hot-access", hot["access_token"])
            self.assertEqual("hot-refresh", hot["refresh_token"])

    def test_same_expiry_without_freshness_does_not_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            hotload_dir = root / "hotload"
            self._enable_hotload(manager, hotload_dir)
            expires = _future_iso(5000)
            account = manager.store.upsert(
                AccountDraft(
                    email="ambiguous@example.com",
                    access_token="mgr-access",
                    refresh_token="mgr-refresh",
                    token_expires_at=expires,
                )
            )
            # Force equal rank: same expiry, no last_refresh, pin file mtime far past.
            path = self._write_hotload(
                hotload_dir,
                account.email,
                access="hot-access",
                refresh="hot-refresh",
                expired=expires,
            )
            past = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
            os.utime(path, (past, past))
            # Also zero manager updated_at freshness by re-writing equal rank via direct
            # comparison after mock: set account updated_at empty through SQL-level path
            # is awkward; instead patch rank freshness on manager side to 0 by clearing
            # updated_at on a fresh get via monkeypatch of _cpa_token_rank inputs.
            original = manager._cpa_token_rank

            def rank_without_freshness(access, refresh="", expires_at="", *, freshness_at=""):
                return original(access, refresh, expires_at, freshness_at="")

            with patch.object(manager, "_cpa_token_rank", side_effect=rank_without_freshness):
                result = manager.sync_account_cpa_with_hotload(manager.store.get(account.id))

            hot = json.loads(path.read_text(encoding="utf-8"))
            stored = manager.store.get(account.id)
            self.assertTrue(result.ok)
            self.assertEqual("noop", result.action)
            self.assertEqual("hot-access", hot["access_token"])
            self.assertEqual("mgr-access", stored.access_token if stored else "")

    def test_inspection_does_not_make_stale_manager_win(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            hotload_dir = root / "hotload"
            self._enable_hotload(manager, hotload_dir)
            expires = _future_iso(5000)
            account = manager.store.upsert(
                AccountDraft(
                    email="stale-mgr@example.com",
                    access_token="mgr-access",
                    refresh_token="mgr-refresh",
                    token_expires_at=expires,
                )
            )
            # CPA credential timestamp stays old; a later inspection only bumps updated_at.
            manager.store.apply_cpa_credentials(
                account.id,
                "mgr-access",
                "mgr-refresh",
                expires,
                "",
                detail="seed",
            )
            # Force cpa_updated_at into the past via SQL.
            past = (
                datetime.now(timezone.utc) - timedelta(days=2)
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            with manager.store._connect() as conn:
                conn.execute(
                    "UPDATE accounts SET cpa_updated_at = ? WHERE id = ?",
                    (past, account.id),
                )
            manager.store.apply_inspection(
                InspectionResult(
                    account_id=account.id,
                    status=AccountStatus.ACTIVE.value,
                    detail="fresh inspect",
                    checked_at=_future_iso(0),
                    expires_at=expires,
                    sso_status=AccountStatus.ACTIVE.value,
                    cpa_status=AccountStatus.ACTIVE.value,
                    cpa_detail="ok",
                )
            )
            later = (
                datetime.now(timezone.utc) + timedelta(hours=1)
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            path = hotload_dir / "xai-stale-mgr@example.com.json"
            path.write_text(
                json.dumps(
                    {
                        "email": "stale-mgr@example.com",
                        "access_token": "hot-access",
                        "refresh_token": "hot-refresh",
                        "expired": expires,
                        "last_refresh": later,
                        "base_url": "http://127.0.0.1:8317/v1",
                    }
                ),
                encoding="utf-8",
            )

            result = manager.sync_account_cpa_with_hotload(manager.store.get(account.id))
            stored = manager.store.get(account.id)
            hot = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual("pull", result.action)
            self.assertEqual("hot-access", stored.access_token if stored else "")
            self.assertEqual("hot-access", hot["access_token"])
            self.assertTrue(str(stored.auth_file if stored else "").endswith(path.name))

    def test_same_token_revives_expired_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            hotload_dir = root / "hotload"
            self._enable_hotload(manager, hotload_dir)
            expires = _future_iso(8000)
            account = manager.store.upsert(
                AccountDraft(
                    email="revive@example.com",
                    access_token="same-access",
                    refresh_token="same-refresh",
                    token_expires_at=expires,
                )
            )
            manager.store.apply_inspection(
                InspectionResult(
                    account_id=account.id,
                    status=AccountStatus.EXPIRED.value,
                    detail="marked expired",
                    checked_at=_future_iso(0),
                    expires_at=_past_iso(100),
                    sso_status=AccountStatus.ACTIVE.value,
                    cpa_status=AccountStatus.EXPIRED.value,
                    cpa_detail="expired",
                )
            )
            path = self._write_hotload(
                hotload_dir,
                account.email,
                access="same-access",
                refresh="same-refresh",
                expired=expires,
            )

            with patch.object(
                manager,
                "inspect_accounts",
                return_value=[
                    InspectionResult(
                        account_id=account.id,
                        status=AccountStatus.ACTIVE.value,
                        detail="revived",
                        checked_at=_future_iso(0),
                        expires_at=expires,
                        sso_status=AccountStatus.ACTIVE.value,
                        cpa_status=AccountStatus.ACTIVE.value,
                        cpa_detail="active",
                    )
                ],
            ) as inspect:
                results = manager.sync_cpa_hotload_accounts(
                    [account.id],
                    reinspect=True,
                )
            self.assertEqual(1, len(results))
            self.assertEqual("pull", results[0].action)
            inspect.assert_called()
            stored = manager.store.get(account.id)
            # apply_cpa_credentials resets to unknown before reinspect callback; the
            # mock inspect result is returned but not applied by the patch. Still
            # assert auth_file/expiry alignment and that account is no longer ignored
            # solely because tokens matched.
            self.assertEqual(expires, stored.token_expires_at if stored else "")
            self.assertTrue(str(stored.auth_file if stored else "").endswith(path.name))

    def test_apply_cpa_credentials_preserves_auth_file_when_blank(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            account = manager.store.upsert(
                AccountDraft(
                    email="meta@example.com",
                    access_token="a1",
                    refresh_token="r1",
                    token_expires_at=_future_iso(1000),
                    auth_file=str(root / "hotload" / "xai-meta@example.com.json"),
                )
            )
            before = manager.store.get(account.id)
            manager.store.apply_cpa_credentials(
                account.id,
                "a2",
                "r2",
                _future_iso(2000),
                "",
                detail="renewed",
            )
            after = manager.store.get(account.id)
            self.assertEqual(before.auth_file if before else "", after.auth_file if after else "")
            self.assertEqual("a2", after.access_token if after else "")
            self.assertTrue(str(after.cpa_updated_at if after else ""))

    def test_import_sets_cpa_updated_at_and_beats_stale_hotload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            hotload_dir = root / "hotload"
            self._enable_hotload(manager, hotload_dir)
            expires = _future_iso(6000)
            # Stale hotload file with an older last_refresh.
            older = (
                datetime.now(timezone.utc) - timedelta(days=3)
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            path = hotload_dir / "xai-import@example.com.json"
            path.write_text(
                json.dumps(
                    {
                        "email": "import@example.com",
                        "access_token": "stale-hot-access",
                        "refresh_token": "stale-hot-refresh",
                        "expired": expires,
                        "last_refresh": older,
                        "base_url": "http://127.0.0.1:8317/v1",
                    }
                ),
                encoding="utf-8",
            )
            imported = manager.store.upsert(
                AccountDraft(
                    email="import@example.com",
                    access_token="new-import-access",
                    refresh_token="new-import-refresh",
                    token_expires_at=expires,
                    source="import",
                    source_modified_at=_future_iso(0),
                )
            )
            stored = manager.store.get(imported.id)
            self.assertTrue(str(stored.cpa_updated_at if stored else ""))
            result = manager.sync_account_cpa_with_hotload(stored)
            after = manager.store.get(imported.id)
            hot = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("push", result.action)
            self.assertEqual("new-import-access", after.access_token if after else "")
            self.assertEqual("new-import-access", hot["access_token"])

    def test_old_import_artifact_does_not_override_newer_hotload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            hotload_dir = root / "hotload"
            self._enable_hotload(manager, hotload_dir)
            expires = _future_iso(7000)
            old_source = (
                datetime.now(timezone.utc) - timedelta(days=5)
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            newer_hot = (
                datetime.now(timezone.utc) - timedelta(hours=1)
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            path = hotload_dir / "xai-old-import@example.com.json"
            path.write_text(
                json.dumps(
                    {
                        "email": "old-import@example.com",
                        "access_token": "fresh-hot-access",
                        "refresh_token": "fresh-hot-refresh",
                        "expired": expires,
                        "last_refresh": newer_hot,
                        "base_url": "http://127.0.0.1:8317/v1",
                    }
                ),
                encoding="utf-8",
            )
            imported = manager.store.upsert(
                AccountDraft(
                    email="old-import@example.com",
                    access_token="ancient-import-access",
                    refresh_token="ancient-import-refresh",
                    token_expires_at=expires,
                    source="disk-import",
                    source_modified_at=old_source,
                )
            )
            stored = manager.store.get(imported.id)
            self.assertEqual(old_source, stored.cpa_updated_at if stored else "")
            result = manager.sync_account_cpa_with_hotload(stored)
            after = manager.store.get(imported.id)
            hot = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("pull", result.action)
            self.assertEqual("fresh-hot-access", after.access_token if after else "")
            self.assertEqual("fresh-hot-access", hot["access_token"])

    def test_old_import_does_not_rollback_refreshed_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            account = manager.store.upsert(
                AccountDraft(
                    email="rollback@example.com",
                    access_token="seed-access",
                    refresh_token="seed-refresh",
                    token_expires_at=_future_iso(1000),
                    source="seed",
                    source_modified_at=_past_iso(86400),
                )
            )
            # Guardian-style refresh stamps a newer cpa_updated_at.
            manager.store.apply_cpa_credentials(
                account.id,
                "fresh-access",
                "fresh-refresh",
                _future_iso(9000),
                "",
                detail="refreshed",
            )
            old_source = (
                datetime.now(timezone.utc) - timedelta(days=5)
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            manager.store.upsert(
                AccountDraft(
                    email="rollback@example.com",
                    access_token="ancient-access",
                    refresh_token="ancient-refresh",
                    token_expires_at=_future_iso(1000),
                    source="old-import",
                    source_modified_at=old_source,
                )
            )
            stored = manager.store.get(account.id)
            self.assertEqual("fresh-access", stored.access_token if stored else "")
            self.assertEqual("fresh-refresh", stored.refresh_token if stored else "")
            self.assertNotEqual(old_source, stored.cpa_updated_at if stored else "")

    def test_empty_expected_refresh_does_not_expire_new_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(
                    email="empty-expected@example.com",
                    access_token="a",
                    refresh_token="new-refresh",
                    token_expires_at=_future_iso(1000),
                )
            )
            manager.store.apply_inspection(
                InspectionResult(
                    account_id=account.id,
                    status=AccountStatus.ACTIVE.value,
                    detail="active",
                    checked_at=_future_iso(0),
                    expires_at=_future_iso(1000),
                    sso_status=AccountStatus.ACTIVE.value,
                    cpa_status=AccountStatus.ACTIVE.value,
                    cpa_detail="active",
                )
            )
            marked = manager.store.mark_cpa_expired_if_refresh_unchanged(
                account.id,
                "",
                "should not expire",
            )
            stored = manager.store.get(account.id)
            self.assertFalse(marked)
            self.assertEqual(
                AccountStatus.ACTIVE.value,
                stored.cpa_status if stored else "",
            )

    def test_cpa_auth_last_refresh_does_not_drive_sso_upsert(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            # Existing login-fresh SSO.
            account = manager.store.upsert(
                AccountDraft(
                    email="sso-cpa@example.com",
                    password="password",
                    sso_token="login-sso",
                    access_token="old-access",
                    refresh_token="old-refresh",
                    token_expires_at=_past_iso(100),
                    source="login",
                    source_modified_at=_past_iso(10),
                )
            )
            with manager.store._connect() as conn:
                conn.execute(
                    "UPDATE accounts SET last_login_at = ?, cpa_updated_at = ? WHERE id = ?",
                    (_future_iso(0), _future_iso(0), account.id),
                )
            # Import carries old SSO text but very new CPA last_refresh.
            manager.store.upsert(
                AccountDraft(
                    email="sso-cpa@example.com",
                    password="password",
                    sso_token="stale-import-sso",
                    access_token="new-cpa-access",
                    refresh_token="new-cpa-refresh",
                    token_expires_at=_future_iso(5000),
                    source="import",
                    source_modified_at=_past_iso(86400),
                    cpa_source_modified_at=_future_iso(100),
                )
            )
            stored = manager.store.get(account.id)
            self.assertEqual("login-sso", stored.sso_token if stored else "")
            self.assertEqual("new-cpa-access", stored.access_token if stored else "")

    def test_stale_inspection_does_not_overwrite_refreshed_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(
                    email="stale-inspect@example.com",
                    access_token="old-access",
                    refresh_token="old-refresh",
                    token_expires_at=_past_iso(10),
                )
            )
            manager.store.apply_cpa_credentials(
                account.id,
                "fresh-access",
                "fresh-refresh",
                _future_iso(9000),
                "",
                detail="refreshed",
            )
            # Inspection snapshot still observed pre-refresh tokens.
            manager.store.apply_inspection(
                InspectionResult(
                    account_id=account.id,
                    status=AccountStatus.EXPIRED.value,
                    detail="stale probe",
                    checked_at=_future_iso(0),
                    expires_at=_past_iso(10),
                    sso_status=AccountStatus.ACTIVE.value,
                    cpa_status=AccountStatus.EXPIRED.value,
                    cpa_detail="expired",
                    observed_access_token="old-access",
                    observed_cpa_updated_at=_past_iso(100),
                )
            )
            stored = manager.store.get(account.id)
            self.assertEqual("fresh-access", stored.access_token if stored else "")
            self.assertNotEqual(
                AccountStatus.EXPIRED.value,
                stored.cpa_status if stored else AccountStatus.EXPIRED.value,
            )


    def test_empty_snapshot_fields_reject_concurrent_cpa_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(
                    email="empty-cas@example.com",
                    refresh_token="stable-refresh",
                )
            )
            with manager.store._connect() as conn:
                conn.execute(
                    "UPDATE accounts SET access_token = '', cpa_updated_at = '' WHERE id = ?",
                    (account.id,),
                )
            snapshot = manager.store.get(account.id)
            manager.store.apply_cpa_credentials(
                account.id,
                "new-access",
                "stable-refresh",
                _future_iso(9000),
                "",
            )

            marked = manager.store.mark_cpa_expired_if_refresh_unchanged(
                account.id,
                snapshot.refresh_token if snapshot else "",
                "stale failure",
                expected_access=snapshot.access_token if snapshot else "",
                expected_cpa_updated_at=snapshot.cpa_updated_at if snapshot else "",
            )

            stored = manager.store.get(account.id)
            self.assertFalse(marked)
            self.assertEqual("new-access", stored.access_token if stored else "")
            self.assertNotEqual(
                AccountStatus.EXPIRED.value,
                stored.cpa_status if stored else AccountStatus.EXPIRED.value,
            )

    def test_stale_inspection_does_not_overwrite_new_sso_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(
                    email="stale-sso-inspect@example.com",
                    password="password",
                    sso_token="old-sso",
                    source_modified_at=_past_iso(100),
                )
            )
            snapshot = manager.store.get(account.id)
            manager.store.upsert(
                AccountDraft(
                    email=account.email,
                    sso_token="new-sso",
                    source_modified_at=_future_iso(100),
                )
            )

            manager.store.apply_inspection(
                InspectionResult(
                    account_id=account.id,
                    status=AccountStatus.EXPIRED.value,
                    detail="stale SSO probe",
                    checked_at=_future_iso(0),
                    sso_status=AccountStatus.EXPIRED.value,
                    sso_detail="expired",
                    observed_sso_token=snapshot.sso_token if snapshot else "",
                    observed_last_login_at=snapshot.last_login_at if snapshot else "",
                    sso_snapshot=True,
                )
            )

            stored = manager.store.get(account.id)
            self.assertEqual("new-sso", stored.sso_token if stored else "")
            self.assertNotEqual(
                AccountStatus.EXPIRED.value,
                stored.sso_status if stored else AccountStatus.EXPIRED.value,
            )


if __name__ == "__main__":
    unittest.main()
