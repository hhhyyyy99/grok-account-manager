"""Lifecycle helpers for project-owned DrissionPage browser profiles."""

from __future__ import annotations

import shutil
import socket
import tempfile
import time
from pathlib import Path
from typing import Any


def browser_profile_path(browser_or_options: Any) -> Path | None:
    """Return the user-data directory exposed by a browser or options object."""
    if browser_or_options is None:
        return None

    if isinstance(browser_or_options, (str, Path)):
        try:
            return Path(browser_or_options).expanduser()
        except (TypeError, ValueError):
            return None

    raw_path = getattr(browser_or_options, "user_data_path", None)
    if not raw_path:
        options = getattr(browser_or_options, "_chromium_options", None)
        raw_path = getattr(options, "user_data_path", None)
    if not raw_path:
        return None

    try:
        return Path(str(raw_path)).expanduser()
    except (TypeError, ValueError):
        return None


def _owned_profile_roots() -> tuple[Path, ...]:
    temp_root = Path(tempfile.gettempdir()).resolve()
    return (
        (temp_root / "DrissionPage" / "autoPortData").resolve(),
        (temp_root / "grok_reg_chrome").resolve(),
    )


def is_project_browser_profile(path: Path | str) -> bool:
    """Only allow deletion below the two temporary roots used by this project."""
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, TypeError, ValueError):
        return False

    for root in _owned_profile_roots():
        if resolved != root and resolved.is_relative_to(root):
            return True
    return False


def _auto_port_profile_is_active(path: Path) -> bool:
    try:
        resolved = path.resolve()
        auto_port_root = _owned_profile_roots()[0]
        if resolved.parent != auto_port_root or not resolved.name.isdigit():
            return False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.1)
            return sock.connect_ex(("127.0.0.1", int(resolved.name))) == 0
    except (OSError, TypeError, ValueError):
        return False


def cleanup_browser_profile(browser_or_options: Any, *, retries: int = 40) -> bool:
    """Remove one project-owned profile after Chromium has released its files."""
    path = browser_profile_path(browser_or_options)
    if path is None or not is_project_browser_profile(path):
        return False

    attempts = max(1, int(retries))
    for attempt in range(attempts):
        if _auto_port_profile_is_active(path):
            return False
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return True
        except OSError:
            if attempt + 1 < attempts:
                time.sleep(0.05)
                continue
        if not path.exists():
            return True
    return not path.exists()


def quit_browser(browser: Any) -> None:
    """Quit Chromium and always clean its project-owned temporary profile."""
    if browser is None:
        return

    profile_path = browser_profile_path(browser)
    quit_succeeded = False
    try:
        browser.quit(del_data=True)
        quit_succeeded = True
    except TypeError:
        try:
            browser.quit()
            quit_succeeded = True
        except Exception:
            pass
    except Exception:
        try:
            browser.quit()
            quit_succeeded = True
        except Exception:
            pass
    finally:
        if quit_succeeded and profile_path is not None:
            cleanup_browser_profile(profile_path)
