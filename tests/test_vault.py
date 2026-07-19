import json
import tempfile
import unittest
from pathlib import Path

from grok_manager.vault import (
    CredentialVault,
    KdfParameters,
    VaultFormatError,
    VaultLockedError,
    VaultPasswordError,
)


TEST_KDF = KdfParameters(memory_cost=8 * 1024, iterations=1, lanes=1)


class CredentialVaultTests(unittest.TestCase):
    def test_initializes_unlocks_and_never_persists_password(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.vault.json"
            vault = CredentialVault(path, kdf_parameters=TEST_KDF)
            vault.initialize("correct horse battery staple")
            ciphertext = vault.encrypt_text("refresh-token-value", "account:1:refresh")

            metadata = path.read_text(encoding="utf-8")
            self.assertNotIn("correct horse battery staple", metadata)
            self.assertNotIn("refresh-token-value", metadata)
            self.assertTrue(ciphertext.startswith("gmv1:"))

            vault.lock()
            with self.assertRaises(VaultLockedError):
                vault.decrypt_text(ciphertext, "account:1:refresh")

            reopened = CredentialVault(path)
            reopened.unlock("correct horse battery staple")
            reopened.put_secret("mail-credential:a@example.com", "mail-token")
            self.assertEqual(
                "mail-token", reopened.get_secret("mail-credential:a@example.com")
            )
            self.assertEqual(
                "refresh-token-value",
                reopened.decrypt_text(ciphertext, "account:1:refresh"),
            )

    def test_rejects_wrong_password_tampering_and_wrong_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.vault.json"
            vault = CredentialVault(path, kdf_parameters=TEST_KDF)
            vault.initialize("correct horse battery staple")
            ciphertext = vault.encrypt_text("secret", "account:1:password")
            vault.lock()

            with self.assertRaises(VaultPasswordError):
                CredentialVault(path).unlock("wrong password value")

            vault.unlock("correct horse battery staple")
            with self.assertRaises(VaultFormatError):
                vault.decrypt_text(ciphertext, "account:2:password")
            replacement = "A" if ciphertext[-1] != "A" else "B"
            with self.assertRaises(VaultFormatError):
                vault.decrypt_text(ciphertext[:-1] + replacement, "account:1:password")

    def test_metadata_contains_versioned_argon2id_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.vault.json"
            CredentialVault(path, kdf_parameters=TEST_KDF).initialize(
                "correct horse battery staple"
            )

            document = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(1, document["version"])
            self.assertEqual("argon2id", document["kdf"]["name"])
            self.assertEqual(8 * 1024, document["kdf"]["memoryCostKiB"])


if __name__ == "__main__":
    unittest.main()
