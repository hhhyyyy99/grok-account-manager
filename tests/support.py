from pathlib import Path

from grok_manager.config import ConfigStore, ManagerConfig
from grok_manager.service import GrokManager
from grok_manager.store import AccountStore


def make_manager(root: Path) -> GrokManager:
    reference_root = root / "reference"
    (reference_root / "grok_register").mkdir(parents=True)
    (reference_root / "register_cli.py").write_text("", encoding="utf-8")
    (reference_root / "config.example.json").write_text("{}\n", encoding="utf-8")

    config_store = ConfigStore(root / "manager-config.json")
    config_store.save(
        ManagerConfig(
            reference_project=str(reference_root),
            reference_python="",
            live_probe=False,
            auto_import_on_start=False,
        )
    )
    return GrokManager(
        config_store=config_store,
        store=AccountStore(root / "accounts.sqlite3"),
    )
