from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("GROK_MANAGER_DATA_DIR", PROJECT_ROOT / "data")).expanduser()
CONFIG_FILE = Path(os.environ.get("GROK_MANAGER_CONFIG", PROJECT_ROOT / "config.json")).expanduser()
DATABASE_FILE = DATA_DIR / "accounts.sqlite3"
JOBS_DIR = DATA_DIR / "jobs"
MANAGED_AUTH_DIR = DATA_DIR / "auths"


def ensure_data_dirs() -> None:
    for path in (DATA_DIR, JOBS_DIR, MANAGED_AUTH_DIR):
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
