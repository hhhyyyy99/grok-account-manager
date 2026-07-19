import sys
from pathlib import Path

from grok_manager.config import ConfigStore, ManagerConfig
from grok_manager.reference import ReferenceProject
from grok_manager.service import GrokManager
from grok_manager.store import AccountStore
from grok_manager.vault import CredentialVault, KdfParameters


TEST_VAULT_KDF = KdfParameters(memory_cost=8 * 1024, iterations=1, lanes=1)


def make_manager(root: Path) -> GrokManager:
    vault = CredentialVault(
        root / "credentials.vault.json",
        kdf_parameters=TEST_VAULT_KDF,
    )
    vault.initialize("test vault password 123")
    reference_root = root / "reference"
    package = reference_root / "grok_register"
    (package / "turnstilePatch").mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text("", encoding="utf-8")
    (package / "turnstilePatch" / "manifest.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    registration_config_example = reference_root / "registration-config.example.json"
    registration_config_example.write_text("{}\n", encoding="utf-8")

    config_store = ConfigStore(root / "manager-config.json")
    config_store.save(
        ManagerConfig(
            live_probe=False,
            auto_import_on_start=False,
        )
    )
    reference = ReferenceProject(
        root=reference_root,
        config_file=root / "registration-config.json",
        config_example_file=registration_config_example,
        output_dir=root / "registration-output",
        data_root=root,
        managed_auth_dir=root / "auths",
    )
    return GrokManager(
        config_store=config_store,
        store=AccountStore(root / "accounts.sqlite3", vault=vault),
        reference=reference,
        python_executable=sys.executable,
    )
