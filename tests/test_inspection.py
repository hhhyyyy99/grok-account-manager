import base64
import json
import tempfile
import unittest
from pathlib import Path

from grok_manager.models import AccountDraft, AccountStatus
from tests.support import make_manager


def jwt_with_exp(expiration: int) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": expiration}).encode("ascii")
    ).decode("ascii").rstrip("=")
    return "header.%s.signature" % payload


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

    def test_credentials_without_sso_need_login(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(Path(directory))
            account = manager.store.upsert(
                AccountDraft(email="missing-sso@example.com", password="password")
            )

            result = manager.inspect_accounts([account.id], live=False)[0]

            self.assertEqual(AccountStatus.NEEDS_LOGIN.value, result.status)


if __name__ == "__main__":
    unittest.main()
