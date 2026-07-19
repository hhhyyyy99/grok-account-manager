import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from grok_manager.models import AccountDraft, AccountStatus, InspectionResult
from grok_register.cpa_xai import oauth_device
from tests.support import make_manager


def _future_iso(seconds: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=seconds)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _mark_cpa(
    manager,
    account_id: int,
    *,
    cpa_status: str,
    expires_at: str,
    overall: str = "",
) -> None:
    manager.store.apply_inspection(
        InspectionResult(
            account_id=account_id,
            status=overall or cpa_status,
            detail="test",
            checked_at=_future_iso(0),
            expires_at=expires_at,
            sso_status=AccountStatus.ACTIVE.value,
            sso_detail="ok",
            cpa_status=cpa_status,
            cpa_detail="test",
        )
    )


class CpaGuardTests(unittest.TestCase):
    def test_selects_only_active_cpa_within_lead_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            soon = manager.store.upsert(
                AccountDraft(
                    email="soon@example.com",
                    access_token="access-soon",
                    refresh_token="refresh-soon",
                    token_expires_at=_future_iso(600),
                )
            )
            later = manager.store.upsert(
                AccountDraft(
                    email="later@example.com",
                    access_token="access-later",
                    refresh_token="refresh-later",
                    token_expires_at=_future_iso(7200),
                )
            )
            expired_status = manager.store.upsert(
                AccountDraft(
                    email="expired-status@example.com",
                    access_token="access-expired",
                    refresh_token="refresh-expired",
                    token_expires_at=_future_iso(300),
                )
            )
            _mark_cpa(
                manager,
                soon.id,
                cpa_status=AccountStatus.ACTIVE.value,
                expires_at=_future_iso(600),
            )
            _mark_cpa(
                manager,
                later.id,
                cpa_status=AccountStatus.ACTIVE.value,
                expires_at=_future_iso(7200),
            )
            _mark_cpa(
                manager,
                expired_status.id,
                cpa_status=AccountStatus.EXPIRED.value,
                expires_at=_future_iso(300),
                overall=AccountStatus.EXPIRED.value,
            )

            selected = manager.cpa_accounts_needing_refresh(lead_seconds=1800)
            selected_ids = {account.id for account in selected}
            self.assertIn(soon.id, selected_ids)
            self.assertNotIn(later.id, selected_ids)
            self.assertNotIn(expired_status.id, selected_ids)

    def test_guard_refreshes_active_and_marks_revoked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            good = manager.store.upsert(
                AccountDraft(
                    email="good@example.com",
                    access_token="old-access",
                    refresh_token="good-refresh",
                    token_expires_at=_future_iso(300),
                )
            )
            bad = manager.store.upsert(
                AccountDraft(
                    email="bad@example.com",
                    access_token="old-access-2",
                    refresh_token="revoked-refresh",
                    token_expires_at=_future_iso(300),
                )
            )
            for account in (good, bad):
                stored = manager.store.get(account.id)
                _mark_cpa(
                    manager,
                    account.id,
                    cpa_status=AccountStatus.ACTIVE.value,
                    expires_at=stored.token_expires_at if stored else _future_iso(300),
                )

            def fake_refresh(refresh_token, **_kwargs):
                if refresh_token == "revoked-refresh":
                    raise oauth_device.OAuthDeviceError(
                        "refresh token failed HTTP 400: invalid_grant"
                    )
                return oauth_device.TokenResult(
                    access_token="fresh-access",
                    refresh_token="fresh-refresh",
                    id_token=None,
                    token_type="Bearer",
                    expires_in=21600,
                    raw={},
                )

            with patch.object(
                oauth_device, "refresh_access_token", side_effect=fake_refresh
            ), patch.object(manager, "inspect_accounts", return_value=[]):
                results = manager.guard_cpa_tokens(lead_seconds=1800)

            by_email = {result.email: result for result in results}
            self.assertTrue(by_email["good@example.com"].ok)
            self.assertFalse(by_email["bad@example.com"].ok)

            good_stored = manager.store.get(good.id)
            bad_stored = manager.store.get(bad.id)
            self.assertEqual("fresh-access", good_stored.access_token if good_stored else "")
            self.assertEqual("fresh-refresh", good_stored.refresh_token if good_stored else "")
            self.assertEqual(
                AccountStatus.EXPIRED.value,
                bad_stored.cpa_status if bad_stored else "",
            )
            self.assertIn("CPA 凭据已过期", bad_stored.cpa_detail if bad_stored else "")
            auth_files = list(manager.reference.managed_auth_dir.glob("xai-*.json"))
            self.assertEqual(1, len(auth_files))
            payload = json.loads(auth_files[0].read_text(encoding="utf-8"))
            self.assertEqual("fresh-access", payload["access_token"])

    def test_guard_loop_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            logs = []
            with patch.object(manager, "guard_cpa_tokens", return_value=[]) as guard:
                manager.run_cpa_guard_loop(
                    interval_seconds=30,
                    lead_seconds=600,
                    once=True,
                    log=logs.append,
                )
            guard.assert_called_once()
            self.assertTrue(any("CPA 守护进程启动" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
