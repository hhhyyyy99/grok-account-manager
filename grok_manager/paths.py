from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent


def _is_source_checkout(package_dir: Path) -> bool:
    root = package_dir.parent
    return (
        (root / "pyproject.toml").is_file()
        and (root / "grok_manager").resolve() == package_dir.resolve()
        and (root / "grok_register").is_dir()
    )


def _user_data_dir(
    platform_name: str,
    environ: Mapping[str, str],
    home: Path,
) -> Path:
    if platform_name == "darwin":
        return home / "Library" / "Application Support" / "grok-account-manager"
    if platform_name == "win32":
        base = environ.get("LOCALAPPDATA") or environ.get("APPDATA")
        if base:
            return Path(base).expanduser() / "Grok Account Manager"
        return home / "AppData" / "Local" / "Grok Account Manager"
    base = environ.get("XDG_DATA_HOME")
    if base:
        return Path(base).expanduser() / "grok-account-manager"
    return home / ".local" / "share" / "grok-account-manager"


def _default_data_dir(
    package_dir: Path = PACKAGE_DIR,
    platform_name: str = sys.platform,
    environ: Mapping[str, str] = os.environ,
    home: Path = Path.home(),
) -> Path:
    if _is_source_checkout(package_dir):
        return package_dir.parent / "data"
    return _user_data_dir(platform_name, environ, home)


IS_SOURCE_CHECKOUT = _is_source_checkout(PACKAGE_DIR)
DATA_DIR = Path(
    os.environ.get("GROK_MANAGER_DATA_DIR", str(_default_data_dir()))
).expanduser()
CONFIG_FILE = Path(
    os.environ.get("GROK_MANAGER_CONFIG", DATA_DIR / "manager-config.json")
).expanduser()
LEGACY_CONFIG_FILE = PROJECT_ROOT / "config.json"
DEFAULT_LEGACY_REFERENCE_ROOT = (
    PROJECT_ROOT.parent / "grok-register-mint"
    if IS_SOURCE_CHECKOUT
    else None
)
LEGACY_INSTALLED_DATA_DIR = None if IS_SOURCE_CHECKOUT else PROJECT_ROOT / "data"
DATABASE_FILE = DATA_DIR / "accounts.sqlite3"
JOBS_DIR = DATA_DIR / "jobs"
MANAGED_AUTH_DIR = DATA_DIR / "auths"
REGISTRATION_CONFIG_FILE = DATA_DIR / "registration-config.json"
REGISTRATION_OUTPUT_DIR = DATA_DIR / "registration-output"
LEGACY_MIGRATION_FILE = DATA_DIR / ".legacy-registration-migration-v1.json"
LEGACY_INSTALL_MIGRATION_FILE = DATA_DIR / ".legacy-install-migration-v1.json"
REGISTRATION_CONFIG_EXAMPLE = (
    PACKAGE_DIR / "registration_config.example.json"
)


def ensure_data_dirs() -> None:
    for path in (DATA_DIR, JOBS_DIR, MANAGED_AUTH_DIR, REGISTRATION_OUTPUT_DIR):
        path.mkdir(parents=True, exist_ok=True)
        try:
            path.chmod(0o700)
        except OSError:
            pass


def write_private_text_atomic(path: Path, content: str, encoding: str = "utf-8") -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = -1
    temp_path: Optional[Path] = None
    try:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".%s." % target.name,
            suffix=".tmp",
            dir=str(target.parent),
            text=True,
        )
        temp_path = Path(temp_name)
        with os.fdopen(descriptor, "w", encoding=encoding) as handle:
            descriptor = -1
            handle.write(content)
        os.replace(str(temp_path), str(target))
        temp_path = None
        try:
            target.chmod(0o600)
        except OSError:
            pass
        return target
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def _copy_missing_tree(source: Path, destination: Path) -> int:
    copied = 0
    for current_root, directories, files in os.walk(source, followlinks=False):
        current = Path(current_root)
        directories[:] = [
            name for name in directories if not (current / name).is_symlink()
        ]
        target_dir = destination / current.relative_to(source)
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            target_dir.chmod(0o700)
        except OSError:
            pass
        for name in files:
            item = current / name
            target = target_dir / name
            if (
                item.is_symlink()
                or not item.is_file()
                or target.exists()
                or name in {"accounts.sqlite3", "accounts.sqlite3-shm", "accounts.sqlite3-wal"}
            ):
                continue
            shutil.copy2(item, target)
            try:
                target.chmod(0o600)
            except OSError:
                pass
            copied += 1
    return copied


def _account_count(database: Path) -> int:
    if not database.is_file():
        return 0
    try:
        connection = sqlite3.connect("file:%s?mode=ro" % database, uri=True)
        try:
            row = connection.execute("SELECT COUNT(*) FROM accounts").fetchone()
            return int(row[0]) if row else 0
        finally:
            connection.close()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return -1


def _backup_account_database(source: Path, destination: Path) -> bool:
    source_count = _account_count(source)
    destination_count = _account_count(destination)
    if source_count <= 0 or destination_count > 0:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect("file:%s?mode=ro" % source, uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    return True


def _migrate_legacy_install_data(
    source_data_dir: Path,
    source_config_file: Path,
    destination_data_dir: Path,
    marker_file: Path,
) -> bool:
    if marker_file.is_file():
        return False
    source_data_dir = Path(source_data_dir)
    source_config_file = Path(source_config_file)
    destination_data_dir = Path(destination_data_dir)
    if not source_data_dir.is_dir() and not source_config_file.is_file():
        return False
    destination_data_dir.mkdir(parents=True, exist_ok=True)
    files_copied = (
        _copy_missing_tree(source_data_dir, destination_data_dir)
        if source_data_dir.is_dir()
        else 0
    )
    database_copied = _backup_account_database(
        source_data_dir / "accounts.sqlite3",
        destination_data_dir / "accounts.sqlite3",
    )
    manager_config = destination_data_dir / "manager-config.json"
    config_copied = False
    if source_config_file.is_file() and not manager_config.exists():
        shutil.copy2(source_config_file, manager_config)
        try:
            manager_config.chmod(0o600)
        except OSError:
            pass
        config_copied = True
    write_private_text_atomic(
        marker_file,
        json.dumps(
            {
                "completed_at": datetime.now(tz=timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
                "source": str(source_data_dir),
                "files_copied": files_copied,
                "database_copied": database_copied,
                "manager_config_copied": config_copied,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    return True


def migrate_legacy_install_data() -> bool:
    if LEGACY_INSTALLED_DATA_DIR is None:
        return False
    return _migrate_legacy_install_data(
        LEGACY_INSTALLED_DATA_DIR,
        LEGACY_CONFIG_FILE,
        DATA_DIR,
        LEGACY_INSTALL_MIGRATION_FILE,
    )
