from __future__ import annotations

import os
import sys
import tempfile
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


DATA_DIR = Path(
    os.environ.get("GROK_MANAGER_DATA_DIR", str(_default_data_dir()))
).expanduser()
CONFIG_FILE = Path(
    os.environ.get("GROK_MANAGER_CONFIG", DATA_DIR / "manager-config.json")
).expanduser()
LEGACY_CONFIG_FILE = PROJECT_ROOT / "config.json"
DEFAULT_LEGACY_REFERENCE_ROOT = (
    PROJECT_ROOT.parent / "grok-register-mint"
    if _is_source_checkout(PACKAGE_DIR)
    else None
)
DATABASE_FILE = DATA_DIR / "accounts.sqlite3"
JOBS_DIR = DATA_DIR / "jobs"
MANAGED_AUTH_DIR = DATA_DIR / "auths"
REGISTRATION_CONFIG_FILE = DATA_DIR / "registration-config.json"
REGISTRATION_OUTPUT_DIR = DATA_DIR / "registration-output"
LEGACY_MIGRATION_FILE = DATA_DIR / ".legacy-registration-migration-v1.json"
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
