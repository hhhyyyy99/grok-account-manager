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
            # Plaintext managed auth files are temporary and must be removed after vault write.
            auth_files = list(manager.reference.managed_auth_dir.glob("xai-*.json"))
            self.assertEqual([], auth_files)

    def test_guard_push_does_not_drop_active_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = make_manager(root)
            hotload_dir = root / "hotload"
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
            account = manager.store.upsert(
                AccountDraft(
                    email="push-active@example.com",
                    access_token="mgr-access",
                    refresh_token="mgr-refresh",
                    token_expires_at=_future_iso(300),
                )
            )
            _mark_cpa(
                manager,
                account.id,
                cpa_status=AccountStatus.ACTIVE.value,
                expires_at=_future_iso(300),
            )
            # Stale hotload forces a manager→hotload push before refresh selection.
            (hotload_dir / "xai-push-active@example.com.json").write_text(
                json.dumps(
                    {
                        "email": "push-active@example.com",
                        "access_token": "stale-access",
                        "refresh_token": "stale-refresh",
                        "expired": _future_iso(100),
                        "base_url": "http://127.0.0.1:8317/v1",
                    }
                ),
                encoding="utf-8",
            )

            def fake_refresh(refresh_token, **_kwargs):
                self.assertEqual("mgr-refresh", refresh_token)
                return oauth_device.TokenResult(
                    access_token="renewed-access",
                    refresh_token="renewed-refresh",
                    id_token=None,
                    token_type="Bearer",
                    expires_in=21600,
                    raw={},
                )

            with patch.object(
                oauth_device, "refresh_access_token", side_effect=fake_refresh
            ), patch.object(manager, "inspect_accounts", return_value=[]):
                results = manager.guard_cpa_tokens(lead_seconds=1800)

            stored = manager.store.get(account.id)
            self.assertTrue(any(item.ok and item.account_id == account.id for item in results))
            self.assertEqual("renewed-access", stored.access_token if stored else "")
            # Push must not wipe cpa_status before refresh; permanent expiry only comes
            # from revoked grants, not from the hotload push path.
            self.assertNotEqual(
                AccountStatus.EXPIRED.value,
                stored.cpa_status if stored else "",
            )

    def test_guard_keeps_active_on_transient_network_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(
                    email="net@example.com",
                    access_token="old-access",
                    refresh_token="still-valid-refresh",
                    token_expires_at=_future_iso(300),
                )
            )
            _mark_cpa(
                manager,
                account.id,
                cpa_status=AccountStatus.ACTIVE.value,
                expires_at=_future_iso(300),
            )

            with patch.object(
                oauth_device,
                "refresh_access_token",
                side_effect=oauth_device.OAuthDeviceError(
                    "refresh token network error: URLError: timed out",
                    retryable=True,
                ),
            ), patch.object(manager, "inspect_accounts", return_value=[]):
                results = manager.guard_cpa_tokens(lead_seconds=1800)

            stored = manager.store.get(account.id)
            self.assertFalse(results[0].ok)
            self.assertIn("暂时失败", results[0].detail)
            self.assertEqual(
                AccountStatus.ACTIVE.value,
                stored.cpa_status if stored else "",
            )
            self.assertEqual(
                "still-valid-refresh",
                stored.refresh_token if stored else "",
            )
            # Next round must still select this active account.
            selected = manager.cpa_accounts_needing_refresh(lead_seconds=1800)
            self.assertIn(account.id, {item.id for item in selected})

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

    def test_oauth_error_bodies_are_redacted(self) -> None:
        body = {
            "error": "invalid_grant",
            "error_description": "revoked",
            "refresh_token": "super-secret-refresh",
            "access_token": "super-secret-access",
            "id_token": "aaa.bbb.ccc.extra-long-jwt-payload-should-mask",
        }
        with self.assertRaises(oauth_device.OAuthDeviceError) as raised:
            oauth_device._token_result_from_body({"refresh_token": "x"})
        # Direct unit coverage for redaction helper used by error formatting.
        formatted = oauth_device._format_oauth_body(body)
        self.assertNotIn("super-secret-refresh", formatted)
        self.assertNotIn("super-secret-access", formatted)
        self.assertIn("'error': 'invalid_grant'", formatted)
        self.assertIn("***", formatted)
        # Network failures are marked retryable so guardians keep candidates.
        err = oauth_device.OAuthDeviceError("network", retryable=True)
        self.assertTrue(err.retryable)
        self.assertIsInstance(raised.exception, oauth_device.OAuthDeviceError)


if __name__ == "__main__":
    unittest.main()
