import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grok_manager import cli
from grok_manager.vault import CredentialVault, KdfParameters, VaultError


TEST_KDF = KdfParameters(memory_cost=8 * 1024, iterations=1, lanes=1)


class VaultEnvUnlockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous = os.environ.get(cli.VAULT_PASSWORD_ENV)
        os.environ.pop(cli.VAULT_PASSWORD_ENV, None)

    def tearDown(self) -> None:
        if self._previous is None:
            os.environ.pop(cli.VAULT_PASSWORD_ENV, None)
        else:
            os.environ[cli.VAULT_PASSWORD_ENV] = self._previous

    def test_load_project_dotenv_sets_missing_keys_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "# comment\n"
                "GROK_MANAGER_VAULT_PASSWORD='env-password-123'\n"
                "OTHER_KEY=from-file\n",
                encoding="utf-8",
            )
            os.environ["OTHER_KEY"] = "already-set"

            cli.load_project_dotenv(env_path)

            self.assertEqual("env-password-123", os.environ[cli.VAULT_PASSWORD_ENV])
            self.assertEqual("already-set", os.environ["OTHER_KEY"])
            os.environ.pop("OTHER_KEY", None)
            os.environ.pop(cli.VAULT_PASSWORD_ENV, None)

    def test_unlock_uses_env_password_without_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault_path = Path(directory) / "credentials.vault.json"
            password = "correct horse battery staple"
            CredentialVault(vault_path, kdf_parameters=TEST_KDF).initialize(password)
            os.environ[cli.VAULT_PASSWORD_ENV] = password

            with patch.object(cli, "load_project_dotenv"), patch.object(
                cli, "VAULT_FILE", vault_path
            ), patch("grok_manager.cli.getpass.getpass") as prompt:
                vault = cli.unlock_vault()

            prompt.assert_not_called()
            self.assertTrue(vault.is_unlocked)
            vault.lock()

    def test_initialize_from_env_skips_confirmation_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault_path = Path(directory) / "credentials.vault.json"
            password = "correct horse battery staple"
            os.environ[cli.VAULT_PASSWORD_ENV] = password

            def make_vault(path, **_kwargs):
                return CredentialVault(path, kdf_parameters=TEST_KDF)

            with patch.object(cli, "load_project_dotenv"), patch.object(
                cli, "VAULT_FILE", vault_path
            ), patch(
                "grok_manager.cli.CredentialVault", side_effect=make_vault
            ), patch("grok_manager.cli.getpass.getpass") as prompt:
                vault = cli.unlock_vault()

            prompt.assert_not_called()
            self.assertTrue(vault_path.is_file())
            self.assertTrue(vault.is_unlocked)
            vault.lock()

    def test_missing_env_falls_back_to_getpass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault_path = Path(directory) / "credentials.vault.json"
            password = "correct horse battery staple"
            CredentialVault(vault_path, kdf_parameters=TEST_KDF).initialize(password)

            with patch.object(cli, "load_project_dotenv"), patch.object(
                cli, "VAULT_FILE", vault_path
            ), patch(
                "grok_manager.cli.getpass.getpass", return_value=password
            ) as prompt:
                vault = cli.unlock_vault()

            prompt.assert_called_once()
            self.assertTrue(vault.is_unlocked)
            vault.lock()

    def test_initialize_rejects_mismatched_interactive_passwords(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault_path = Path(directory) / "credentials.vault.json"
            with patch.object(cli, "load_project_dotenv"), patch.object(
                cli, "VAULT_FILE", vault_path
            ), patch(
                "grok_manager.cli.getpass.getpass",
                side_effect=["first password!", "second password!"],
            ):
                with self.assertRaisesRegex(VaultError, "不一致"):
                    cli.unlock_vault()


if __name__ == "__main__":
    unittest.main()
