"""Writable data and packaged asset paths for the embedded registration runtime."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from threading import Lock


def _environment_path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, str(default))).expanduser().resolve()


PACKAGE_DIR = Path(__file__).resolve().parent
try:
    from grok_manager.paths import (
        DATA_DIR as MANAGER_DATA_DIR,
        REGISTRATION_CONFIG_EXAMPLE as MANAGER_CONFIG_EXAMPLE,
    )
except ImportError:
    MANAGER_DATA_DIR = PACKAGE_DIR.parent / "data"
    MANAGER_CONFIG_EXAMPLE = PACKAGE_DIR.parent / "config.example.json"

PROJECT_ROOT = _environment_path("GROK_REGISTER_PROJECT_ROOT", MANAGER_DATA_DIR)

CONFIG_FILE = _environment_path(
    "GROK_REGISTER_CONFIG_FILE",
    PROJECT_ROOT / "registration-config.json",
)
CONFIG_EXAMPLE = _environment_path(
    "GROK_REGISTER_CONFIG_EXAMPLE",
    MANAGER_CONFIG_EXAMPLE,
)
OUTPUT_DIR = _environment_path(
    "GROK_REGISTER_OUTPUT_DIR",
    PROJECT_ROOT / "registration-output",
)
TURNSTILE_DIR = _environment_path(
    "GROK_REGISTER_TURNSTILE_DIR",
    PACKAGE_DIR / "turnstilePatch",
)
CRASH_LOG_FILE = _environment_path(
    "GROK_REGISTER_CRASH_LOG",
    PROJECT_ROOT / "registration-crash.log",
)
TOKEN_JSON = _environment_path(
    "GROK_REGISTER_TOKEN_FILE",
    PROJECT_ROOT / "registration-token.json",
)

_batch_lock = Lock()
_active_batch_dir: Path | None = None


def ensure_output_dir() -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR


def create_batch_output_dir(now: datetime | None = None) -> Path:
    """Create and activate one timestamped output directory for this run."""
    global _active_batch_dir
    root = ensure_output_dir()
    timestamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    with _batch_lock:
        candidate = root / f"out_{timestamp}"
        suffix = 2
        while candidate.exists():
            candidate = root / f"out_{timestamp}_{suffix}"
            suffix += 1
        candidate.mkdir(parents=True)
        _active_batch_dir = candidate.resolve()
        return _active_batch_dir


def get_active_batch_dir() -> Path | None:
    return _active_batch_dir


def current_output_dir() -> Path:
    return _active_batch_dir or ensure_output_dir()


def current_output_path(*parts: str) -> Path:
    return current_output_dir().joinpath(*parts)
