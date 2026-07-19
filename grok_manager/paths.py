from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Mapping, Optional


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
VAULT_FILE = DATA_DIR / "credentials.vault.json"
JOBS_DIR = DATA_DIR / "jobs"
MANAGED_AUTH_DIR = DATA_DIR / "auths"
REGISTRATION_CONFIG_FILE = DATA_DIR / "registration-config.json"
REGISTRATION_OUTPUT_DIR = DATA_DIR / "registration-output"
REGISTRATION_PATH_KEYS = frozenset(
    {
        "cpa_auth_dir",
        "cpa_hotload_dir",
        "grok2api_local_token_file",
        "sub2api_combined_file",
        "sub2api_export_dir",
    }
)
LEGACY_MIGRATION_FILE = DATA_DIR / ".legacy-registration-migration-v2.json"
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


@contextmanager
def interprocess_lock(name: str, data_root: Path | None = None) -> Iterator[None]:
    """Cross-process exclusive lock for CPA sync/guardian (Unix fcntl / Windows msvcrt)."""
    root = Path(data_root or DATA_DIR).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / (".%s.lock" % str(name or "lock").strip().replace("/", "_"))
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass
        if sys.platform == "win32":
            import msvcrt

            # Lock one byte at the start of the file.
            handle.seek(0)
            if handle.read(1) == "":
                handle.write("0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def remove_managed_auth_file(
    auth_file: str | Path | None,
    managed_auth_dir: Path | None = None,
) -> None:
    """Delete a transient plaintext xai-*.json under the managed auth directory."""
    if not auth_file:
        return
    root = Path(managed_auth_dir or MANAGED_AUTH_DIR).expanduser().resolve()
    try:
        target = Path(auth_file).expanduser().resolve()
        target.relative_to(root)
        target.unlink(missing_ok=True)
    except (OSError, ValueError):
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
        connection = sqlite3.connect(
            database.resolve().as_uri() + "?mode=ro",
            uri=True,
        )
        try:
            row = connection.execute("SELECT COUNT(*) FROM accounts").fetchone()
            return int(row[0]) if row else 0
        finally:
            connection.close()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return -1


def _rebase_database_auth_files(
    database: Path,
    source_root: Path,
    destination_root: Path,
) -> int:
    connection = sqlite3.connect(database)
    try:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(accounts)")
        }
        if "id" not in columns or "auth_file" not in columns:
            return 0
        updates = []
        for account_id, raw in connection.execute(
            "SELECT id, auth_file FROM accounts WHERE auth_file != ''"
        ):
            path = Path(str(raw)).expanduser()
            if not path.is_absolute():
                continue
            try:
                relative = path.resolve().relative_to(source_root.resolve())
            except ValueError:
                continue
            updates.append((str(destination_root / relative), int(account_id)))
        if updates:
            with connection:
                connection.executemany(
                    "UPDATE accounts SET auth_file = ? WHERE id = ?",
                    updates,
                )
        return len(updates)
    finally:
        connection.close()


def _migrate_account_database(source: Path, destination: Path) -> tuple[bool, int]:
    source_count = _account_count(source)
    destination_count = _account_count(destination)
    if source_count < 0:
        raise ValueError("旧账号数据库无法读取: %s" % source)
    if source_count == 0:
        return True, 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination_count <= 0:
        source_connection = sqlite3.connect(
            source.resolve().as_uri() + "?mode=ro",
            uri=True,
        )
        destination_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(destination_connection)
        finally:
            destination_connection.close()
            source_connection.close()
        migrated_count = source_count
    else:
        source_connection = sqlite3.connect(
            source.resolve().as_uri() + "?mode=ro",
            uri=True,
        )
        destination_connection = sqlite3.connect(destination)
        source_connection.row_factory = sqlite3.Row
        destination_connection.row_factory = sqlite3.Row
        try:
            source_columns = {
                str(row[1])
                for row in source_connection.execute("PRAGMA table_info(accounts)")
            }
            destination_columns = {
                str(row[1])
                for row in destination_connection.execute("PRAGMA table_info(accounts)")
            }
            expected_columns = (
                "email",
                "password",
                "sso_token",
                "access_token",
                "refresh_token",
                "token_expires_at",
                "sso_expires_at",
                "auth_file",
                "source",
                "source_modified_at",
                "status",
                "status_detail",
                "sso_status",
                "sso_detail",
                "cpa_status",
                "cpa_detail",
                "cpa_updated_at",
                "last_checked_at",
                "last_login_at",
                "created_at",
                "updated_at",
            )
            columns = [
                name
                for name in expected_columns
                if name in source_columns and name in destination_columns
            ]
            if "email" not in columns:
                raise ValueError("账号数据库缺少 email 列")
            fill_empty_columns = {
                "password",
                "sso_token",
                "access_token",
                "refresh_token",
                "token_expires_at",
                "sso_expires_at",
                "auth_file",
                "source",
                "source_modified_at",
                "last_checked_at",
                "last_login_at",
            }
            source_rows = source_connection.execute(
                "SELECT %s FROM accounts" % ", ".join(columns)
            ).fetchall()

            def migrated_value(row: sqlite3.Row, name: str):
                value = row[name]
                if name != "auth_file" or not str(value or "").strip():
                    return value
                auth_path = Path(str(value)).expanduser()
                if not auth_path.is_absolute():
                    return value
                try:
                    relative = auth_path.resolve().relative_to(source.parent.resolve())
                except ValueError:
                    return value
                return str(destination.parent / relative)

            migrated_count = 0
            with destination_connection:
                for row in source_rows:
                    email = str(row["email"] or "").strip().lower()
                    if not email:
                        continue
                    existing = destination_connection.execute(
                        "SELECT * FROM accounts WHERE email = ? COLLATE NOCASE",
                        (email,),
                    ).fetchone()
                    if existing is None:
                        placeholders = ", ".join("?" for _ in columns)
                        destination_connection.execute(
                            "INSERT INTO accounts (%s) VALUES (%s)"
                            % (", ".join(columns), placeholders),
                            [migrated_value(row, name) for name in columns],
                        )
                        migrated_count += 1
                        continue
                    updates = {
                        name: migrated_value(row, name)
                        for name in columns
                        if name in fill_empty_columns
                        and not str(existing[name] or "").strip()
                        and str(row[name] or "").strip()
                    }
                    if updates:
                        destination_connection.execute(
                            "UPDATE accounts SET %s WHERE email = ? COLLATE NOCASE"
                            % ", ".join("%s = ?" % name for name in updates),
                            list(updates.values()) + [email],
                        )
                        migrated_count += 1
        finally:
            destination_connection.close()
            source_connection.close()
    _rebase_database_auth_files(destination, source.parent, destination.parent)
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    return True, migrated_count


def _rebase_registration_config(
    config_file: Path,
    source_root: Path,
    destination_root: Path,
) -> None:
    if not config_file.is_file():
        return
    try:
        values = json.loads(config_file.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("迁移后的注册配置无法读取: %s" % exc) from exc
    if not isinstance(values, dict):
        raise ValueError("迁移后的注册配置必须是 JSON 对象")
    changed = False
    for key in REGISTRATION_PATH_KEYS:
        raw = str(values.get(key) or "").strip()
        if not raw:
            continue
        path = Path(raw).expanduser()
        if not path.is_absolute():
            continue
        try:
            relative = path.resolve().relative_to(source_root.resolve())
        except ValueError:
            continue
        values[key] = str(destination_root / relative)
        changed = True
    if changed:
        write_private_text_atomic(
            config_file,
            json.dumps(values, ensure_ascii=False, indent=2) + "\n",
        )


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
    source_database = source_data_dir / "accounts.sqlite3"
    if source_database.is_file():
        database_migrated, accounts_migrated = _migrate_account_database(
            source_database,
            destination_data_dir / "accounts.sqlite3",
        )
    else:
        database_migrated, accounts_migrated = True, 0
    manager_config = destination_data_dir / "manager-config.json"
    config_copied = False
    if source_config_file.is_file() and not manager_config.exists():
        shutil.copy2(source_config_file, manager_config)
        try:
            manager_config.chmod(0o600)
        except OSError:
            pass
        config_copied = True
    _rebase_registration_config(
        destination_data_dir / "registration-config.json",
        source_data_dir,
        destination_data_dir,
    )
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
                "database_migrated": database_migrated,
                "accounts_migrated": accounts_migrated,
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
