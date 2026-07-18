from __future__ import annotations

import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .models import AccountDraft
from .vault import CredentialVault
from .paths import (
    DATA_DIR,
    DEFAULT_LEGACY_REFERENCE_ROOT,
    LEGACY_CONFIG_FILE,
    LEGACY_MIGRATION_FILE,
    MANAGED_AUTH_DIR,
    PROJECT_ROOT,
    REGISTRATION_CONFIG_EXAMPLE,
    REGISTRATION_CONFIG_FILE,
    REGISTRATION_OUTPUT_DIR,
    REGISTRATION_PATH_KEYS,
    ensure_data_dirs,
    write_private_text_atomic,
)


LogCallback = Callable[[str], None]


SENSITIVE_CONFIG_KEYS = frozenset(
    {
        "duckmail_api_key",
        "cloudflare_api_key",
        "proxy",
        "yyds_api_key",
        "yyds_jwt",
        "grok2api_remote_app_key",
        "cpa_proxy",
        "cpa_cloud_management_key",
    }
)


def _is_supported_python(version_text: str) -> bool:
    matched = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(version_text or "").strip())
    return bool(
        matched and (int(matched.group(1)), int(matched.group(2))) == (3, 13)
    )


@dataclass(frozen=True)
class RegistrationRequest:
    count: int
    threads: int
    mint_workers: int = 1


@dataclass(frozen=True)
class RegistrationResult:
    ok: bool
    return_code: int
    accounts_file: str
    batch_dir: str
    imported_count: int = 0
    error: str = ""


class ReferenceProjectError(RuntimeError):
    pass


class ReferenceProject:
    """Paths and environment for the registration runtime embedded in this package."""

    def __init__(
        self,
        root: Path = PROJECT_ROOT,
        config_file: Path = REGISTRATION_CONFIG_FILE,
        config_example_file: Path = REGISTRATION_CONFIG_EXAMPLE,
        output_dir: Path = REGISTRATION_OUTPUT_DIR,
        data_root: Path = DATA_DIR,
        managed_auth_dir: Path = MANAGED_AUTH_DIR,
        legacy_manager_config_file: Optional[Path] = None,
        legacy_reference_root: Optional[Path] = None,
        migration_file: Optional[Path] = None,
    ):
        self.root = Path(root).expanduser().resolve()
        self._config_file = Path(config_file).expanduser().resolve()
        self._config_example_file = Path(config_example_file).expanduser().resolve()
        self._output_dir = Path(output_dir).expanduser().resolve()
        self.data_root = Path(data_root).expanduser().resolve()
        self.managed_auth_dir = Path(managed_auth_dir).expanduser().resolve()
        default_layout = (
            self.root == PROJECT_ROOT.resolve() and self.data_root == DATA_DIR.resolve()
        )
        legacy_source_layout = (
            default_layout and DEFAULT_LEGACY_REFERENCE_ROOT is not None
        )
        self.legacy_manager_config_file = (
            Path(legacy_manager_config_file).expanduser().resolve()
            if legacy_manager_config_file is not None
            else (LEGACY_CONFIG_FILE.resolve() if default_layout else None)
        )
        self.legacy_reference_root = (
            Path(legacy_reference_root).expanduser().resolve()
            if legacy_reference_root is not None
            else (
                DEFAULT_LEGACY_REFERENCE_ROOT.resolve()
                if legacy_source_layout and DEFAULT_LEGACY_REFERENCE_ROOT is not None
                else None
            )
        )
        self.migration_file = (
            Path(migration_file).expanduser().resolve()
            if migration_file is not None
            else (
                LEGACY_MIGRATION_FILE.resolve()
                if default_layout
                else self.data_root / ".legacy-registration-migration-v2.json"
            )
        )
        self.credential_vault = None

    @property
    def entrypoint(self) -> Path:
        return self.root / "grok_register" / "cli.py"

    @property
    def config_file(self) -> Path:
        return self._config_file

    @property
    def config_example_file(self) -> Path:
        return self._config_example_file

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    @property
    def work_dir(self) -> Path:
        self.data_root.mkdir(parents=True, exist_ok=True)
        return self.data_root

    @property
    def turnstile_dir(self) -> Path:
        return self.root / "grok_register" / "turnstilePatch"

    def environment(self, base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        env = dict(base or os.environ)
        config = self.load_registration_config()
        runtime_secrets = {
            key: str(config.get(key) or "")
            for key in SENSITIVE_CONFIG_KEYS
            if str(config.get(key) or "")
        }
        env.update(
            {
                "GROK_REGISTER_PROJECT_ROOT": str(self.data_root),
                "GROK_REGISTER_CONFIG_FILE": str(self.config_file),
                "GROK_REGISTER_CONFIG_EXAMPLE": str(self.config_example_file),
                "GROK_REGISTER_OUTPUT_DIR": str(self.output_dir),
                "GROK_REGISTER_TURNSTILE_DIR": str(self.turnstile_dir),
                "GROK_REGISTER_CRASH_LOG": str(self.data_root / "registration-crash.log"),
                "GROK_REGISTER_TOKEN_FILE": str(self.data_root / "registration-token.json"),
                "GROK_REGISTER_CONFIG_SECRETS": json.dumps(runtime_secrets, ensure_ascii=True),
            }
        )
        existing_path = env.get("PYTHONPATH", "").strip()
        env["PYTHONPATH"] = str(self.root) + (
            os.pathsep + existing_path if existing_path else ""
        )
        return env

    def validate(self) -> None:
        missing = []
        for path in (
            self.entrypoint,
            self.config_example_file,
            self.turnstile_dir / "manifest.json",
        ):
            if not path.exists():
                missing.append(str(path))
        if missing:
            raise ReferenceProjectError("内置注册运行时不完整，缺少: %s" % ", ".join(missing))

    def load_registration_config(self) -> Dict[str, Any]:
        self.validate()
        base: Dict[str, Any] = {}
        try:
            example = json.loads(self.config_example_file.read_text(encoding="utf-8-sig"))
            if isinstance(example, dict):
                base.update(example)
        except (OSError, json.JSONDecodeError) as exc:
            raise ReferenceProjectError("参考配置模板读取失败: %s" % exc) from exc
        if self.config_file.is_file():
            try:
                local = json.loads(self.config_file.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ReferenceProjectError("注册配置读取失败: %s" % exc) from exc
            if not isinstance(local, dict):
                raise ReferenceProjectError("注册配置必须是 JSON 对象")
            base.update(local)
        vault = self.credential_vault
        for key in SENSITIVE_CONFIG_KEYS:
            raw = str(base.get(key) or "")
            if not raw:
                continue
            if not CredentialVault.is_encrypted(raw):
                continue
            if vault is None or not vault.is_unlocked:
                raise ReferenceProjectError("读取受保护注册配置前必须解锁保险库")
            try:
                base[key] = vault.decrypt_text(raw, "registration-config:%s" % key)
            except Exception as exc:
                raise ReferenceProjectError("注册配置密文无法解密: %s" % key) from exc
        return base

    def save_registration_config(self, values: Dict[str, Any]) -> Path:
        if not isinstance(values, dict):
            raise ValueError("注册配置必须是 JSON 对象")
        self.validate()
        existing: Dict[str, Any] = {}
        if self.config_file.is_file():
            try:
                raw_existing = json.loads(self.config_file.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ReferenceProjectError("现有注册配置读取失败: %s" % exc) from exc
            if not isinstance(raw_existing, dict):
                raise ReferenceProjectError("现有注册配置必须是 JSON 对象")
            existing = raw_existing
        document = dict(existing)
        document.update(values)
        vault = self.credential_vault
        for key in SENSITIVE_CONFIG_KEYS:
            if key in values and values.get(key) is None:
                document[key] = ""
                continue
            incoming = str(values.get(key) or "") if key in values else ""
            existing_raw = str(existing.get(key) or "")
            if key in values and not incoming and existing_raw:
                document[key] = existing_raw
                continue
            raw = str(document.get(key) or "")
            if not raw:
                continue
            if vault is None or not vault.is_unlocked:
                raise ReferenceProjectError("保存受保护注册配置前必须解锁保险库")
            if not vault.is_encrypted(raw):
                document[key] = vault.encrypt_text(raw, "registration-config:%s" % key)
        return write_private_text_atomic(
            self.config_file,
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        )
    def ensure_registration_config(self) -> Path:
        if self.config_file.is_file():
            return self.config_file
        return self.save_registration_config(self.load_registration_config())

    def sync_cpa_hotload(self, auth_file: str | Path) -> Optional[Path]:
        config = self.load_registration_config()
        if not bool(config.get("cpa_copy_to_hotload", False)):
            return None

        configured = str(config.get("cpa_hotload_dir") or "").strip()
        if not configured:
            raise ReferenceProjectError(
                "已启用 CPA hotload，但 cpa_hotload_dir 为空"
            )

        source = Path(auth_file).expanduser().resolve()
        if not source.is_file():
            raise ReferenceProjectError("CPA auth 文件不存在: %s" % source)

        target_dir = Path(configured).expanduser()
        if not target_dir.is_absolute():
            target_dir = self.data_root / target_dir
        target_dir = target_dir.resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
        destination = target_dir / source.name
        if source != destination.resolve():
            shutil.copy2(source, destination)
        try:
            destination.chmod(0o600)
        except OSError:
            pass
        return destination

    def sync_grok2api(
        self,
        sso_token: str,
        email: str = "",
        log_callback: Optional[LogCallback] = None,
    ) -> None:
        config = self.load_registration_config()
        if not bool(config.get("grok2api_auto_add_local", True)) and not bool(
            config.get("grok2api_auto_add_remote", False)
        ):
            return
        configured = str(config.get("grok2api_local_token_file") or "").strip()
        if configured:
            token_file = Path(configured).expanduser()
            if not token_file.is_absolute():
                token_file = self.data_root / token_file
            config["grok2api_local_token_file"] = str(token_file.resolve())

        from grok_register.app import add_token_to_grok2api_pools

        add_token_to_grok2api_pools(
            sso_token,
            email=email,
            log_callback=log_callback,
            settings=config,
            default_token_file=self.data_root / "registration-token.json",
            replace_email=True,
        )

    def _legacy_root_from_manager_config(self) -> Tuple[Optional[Path], bool]:
        root = self.legacy_reference_root
        path = self.legacy_manager_config_file
        if path is None or not path.is_file():
            return root, False
        try:
            values = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReferenceProjectError("旧管理端配置读取失败: %s" % exc) from exc
        if not isinstance(values, dict):
            raise ReferenceProjectError("旧管理端配置必须是 JSON 对象")
        configured = str(values.get("reference_project") or "").strip()
        if not configured:
            return root, False
        configured_path = Path(configured).expanduser()
        if not configured_path.is_absolute():
            configured_path = path.parent / configured_path
        return configured_path.resolve(), True

    def _registration_config_is_default(self) -> bool:
        if not self.config_file.is_file():
            return True
        try:
            current = json.loads(self.config_file.read_text(encoding="utf-8-sig"))
            example = json.loads(self.config_example_file.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return False
        return current == example

    def _rebase_legacy_config_paths(
        self,
        values: Dict[str, Any],
        legacy_root: Path,
    ) -> Dict[str, Any]:
        migrated = dict(values)
        for key in REGISTRATION_PATH_KEYS:
            raw = str(migrated.get(key) or "").strip()
            if not raw:
                continue
            source = Path(raw).expanduser()
            if not source.is_absolute():
                continue
            try:
                relative = source.resolve().relative_to(legacy_root)
            except ValueError:
                continue
            if relative.parts and relative.parts[0] == "output":
                target = (self.output_dir / "legacy-import").joinpath(
                    *relative.parts[1:]
                )
            else:
                target = self.data_root / "legacy-files" / relative
            migrated[key] = str(target)
        return migrated

    @staticmethod
    def _copy_legacy_output(source: Path, destination: Path) -> int:
        copied = 0
        for current_root, directories, files in os.walk(source, followlinks=False):
            current = Path(current_root)
            directories[:] = [
                name for name in directories if not (current / name).is_symlink()
            ]
            relative = current.relative_to(source)
            target_dir = destination / relative
            target_dir.mkdir(parents=True, exist_ok=True)
            try:
                target_dir.chmod(0o700)
            except OSError:
                pass
            for name in files:
                item = current / name
                if item.is_symlink() or not item.is_file():
                    continue
                target = target_dir / name
                shutil.copy2(item, target)
                try:
                    target.chmod(0o600)
                except OSError:
                    pass
                copied += 1
        return copied

    def migrate_legacy_data(self) -> None:
        """Copy legacy sibling data once, leaving no ongoing runtime dependency."""
        if self.migration_file.is_file():
            return
        ensure_data_dirs()
        legacy_root, explicit_source = self._legacy_root_from_manager_config()
        result: Dict[str, Any] = {
            "completed_at": datetime.now(tz=timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "source": str(legacy_root) if legacy_root is not None else "",
            "config_copied": False,
            "output_files_copied": 0,
            "database_auth_paths_rebased": 0,
        }
        if legacy_root is not None and legacy_root != self.root:
            result["database_auth_paths_rebased"] = self._rebase_database_auth_paths(
                legacy_root
            )
            legacy_config = legacy_root / "config.json"
            legacy_output = legacy_root / "output"
            if not legacy_config.is_file() and not legacy_output.is_dir():
                if explicit_source:
                    return
            if legacy_config.is_file() and self._registration_config_is_default():
                try:
                    values = json.loads(legacy_config.read_text(encoding="utf-8-sig"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ReferenceProjectError("旧注册配置读取失败: %s" % exc) from exc
                if not isinstance(values, dict):
                    raise ReferenceProjectError("旧注册配置必须是 JSON 对象")
                self.save_registration_config(
                    self._rebase_legacy_config_paths(values, legacy_root)
                )
                result["config_copied"] = True
            if legacy_output.is_dir():
                result["output_files_copied"] = self._copy_legacy_output(
                    legacy_output,
                    self.output_dir / "legacy-import",
                )
        write_private_text_atomic(
            self.migration_file,
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        )

    def _rebase_database_auth_paths(self, legacy_root: Path) -> int:
        database = self.data_root / "accounts.sqlite3"
        if not database.is_file():
            return 0
        connection = sqlite3.connect(database)
        try:
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(accounts)")
            }
            if "id" not in columns or "auth_file" not in columns:
                return 0
            updates = []
            for account_id, email, encoded_path in connection.execute(
                "SELECT id, email, auth_file FROM accounts WHERE auth_file != ''"
            ):
                vault = getattr(self, "credential_vault", None)
                if vault is not None and vault.is_encrypted(str(encoded_path)):
                    path_text = vault.decrypt_text(
                        str(encoded_path), "account:%s:auth_file" % str(email).strip().lower()
                    )
                else:
                    path_text = str(encoded_path)
                path = Path(path_text).expanduser()
                if not path.is_absolute():
                    continue
                try:
                    relative = path.resolve().relative_to(legacy_root / "output")
                except ValueError:
                    continue
                updates.append(
                    (
                        vault.encrypt_text(str((self.output_dir / "legacy-import" / relative).resolve()), "account:%s:auth_file" % str(email).strip().lower()) if vault is not None and vault.is_unlocked else str((self.output_dir / "legacy-import" / relative).resolve()),
                        int(account_id),
                    )
                )
            if updates:
                with connection:
                    connection.executemany(
                        "UPDATE accounts SET auth_file = ? WHERE id = ?",
                        updates,
                    )
            return len(updates)
        finally:
            connection.close()

    def discover_account_files(self) -> List[Path]:
        candidates: Dict[str, Path] = {}
        patterns = ("**/accounts*.txt",)
        for pattern in patterns:
            for path in self.output_dir.glob(pattern):
                if path.is_file():
                    candidates[str(path.resolve())] = path.resolve()
        return sorted(candidates.values(), key=lambda item: (item.stat().st_mtime, str(item)))

    def discover_mail_credential_files(self) -> List[Path]:
        candidates: Dict[str, Path] = {}
        for path in self.output_dir.glob("**/mail_credentials.txt"):
            if path.is_file():
                candidates[str(path.resolve())] = path.resolve()
        return sorted(
            candidates.values(),
            key=lambda item: (item.stat().st_mtime, str(item)),
            reverse=True,
        )

    @staticmethod
    def _mail_credential_from_file(path: Path, email: str) -> str:
        target = str(email or "").strip().casefold()
        if not target or not Path(path).is_file():
            return ""
        try:
            lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        for line in reversed(lines):
            address, separator, credential = line.partition("\t")
            if separator and address.strip().casefold() == target:
                return credential.strip()
        return ""

    def find_mail_credential(self, email: str, source: str = "") -> str:
        vault = self.credential_vault
        normalized_email = str(email or "").strip().lower()
        if vault is not None and vault.is_unlocked:
            try:
                stored = vault.get_secret("mail-credential:%s" % normalized_email)
            except Exception:
                stored = ""
            if stored:
                return stored
        checked = set()
        source_path = Path(str(source or "")).expanduser()
        if source_path.is_file():
            sibling = (source_path.parent / "mail_credentials.txt").resolve()
            checked.add(str(sibling))
            credential = self._mail_credential_from_file(sibling, email)
            if credential:
                if vault is not None and vault.is_unlocked:
                    vault.put_secret("mail-credential:%s" % normalized_email, credential)
                return credential
        for path in self.discover_mail_credential_files():
            if str(path) in checked:
                continue
            credential = self._mail_credential_from_file(path, email)
            if credential:
                if vault is not None and vault.is_unlocked:
                    vault.put_secret("mail-credential:%s" % normalized_email, credential)
                return credential
        return ""

    def persist_account_password(self, email: str, password: str, source: str = "") -> Path:
        normalized_email = str(email or "").strip().lower()
        normalized_password = str(password or "").strip()
        if not normalized_email or not normalized_password:
            raise ValueError("邮箱和新密码不能为空")
        vault = self.credential_vault
        if vault is None or not vault.is_unlocked:
            raise ReferenceProjectError("保存账号密码前必须解锁保险库")
        vault.put_secret("account-password:%s" % normalized_email, normalized_password)
        return vault.path
    def discover_auth_files(self, extra_dirs: Sequence[Path] = ()) -> List[Path]:
        candidates: Dict[str, Path] = {}
        for path in self.output_dir.glob("**/xai-*.json"):
            if path.is_file():
                candidates[str(path.resolve())] = path.resolve()
        try:
            configured = str(self.load_registration_config().get("cpa_auth_dir") or "").strip()
        except ReferenceProjectError:
            configured = ""
        if configured:
            auth_dir = Path(configured).expanduser()
            if not auth_dir.is_absolute():
                auth_dir = self.data_root / auth_dir
            for path in auth_dir.glob("xai-*.json"):
                if path.is_file():
                    candidates[str(path.resolve())] = path.resolve()
        for path in self.managed_auth_dir.glob("xai-*.json"):
            if path.is_file():
                candidates[str(path.resolve())] = path.resolve()
        for extra_dir in extra_dirs:
            directory = Path(extra_dir)
            if directory.is_file():
                directory = directory.parent
            if not directory.is_dir():
                continue
            for path in directory.glob("**/xai-*.json"):
                if path.is_file():
                    candidates[str(path.resolve())] = path.resolve()
        return sorted(candidates.values(), key=lambda item: (item.stat().st_mtime, str(item)))

    @staticmethod
    def _load_auth(path: Path) -> Optional[Tuple[str, Dict[str, Any]]]:
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            return None
        email = str(value.get("email") or "").strip().lower()
        if not email and path.name.startswith("xai-"):
            email = path.stem[4:].strip().lower()
        if not email:
            return None
        return email, value

    def build_auth_index(self, extra_dirs: Sequence[Path] = ()) -> Dict[str, Tuple[Path, Dict[str, Any]]]:
        result: Dict[str, Tuple[Path, Dict[str, Any]]] = {}
        for path in self.discover_auth_files(extra_dirs):
            loaded = self._load_auth(path)
            if loaded is None:
                continue
            email, payload = loaded
            result[email] = (path, payload)
        return result

    @staticmethod
    def parse_account_file(
        path: Path,
        auth_index: Optional[Dict[str, Tuple[Path, Dict[str, Any]]]] = None,
    ) -> List[AccountDraft]:
        path = Path(path)
        if not path.is_file():
            return []
        return ReferenceProject.parse_account_text(
            path.read_text(encoding="utf-8", errors="replace"),
            source=str(path),
            source_modified_at=(
                datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            ),
            auth_index=auth_index,
        )

    @staticmethod
    def parse_account_text(
        text: str,
        source: str = "manual-import",
        source_modified_at: str = "",
        auth_index: Optional[Dict[str, Tuple[Path, Dict[str, Any]]]] = None,
    ) -> List[AccountDraft]:
        auth_index = auth_index or {}
        records: List[AccountDraft] = []
        for line in str(text or "").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            parts = raw.split("----", 2)
            if len(parts) < 2:
                continue
            email = parts[0].strip().lower()
            password = parts[1].strip()
            sso = parts[2].strip() if len(parts) > 2 else ""
            if not email or not password:
                continue
            auth_path = ""
            auth: Dict[str, Any] = {}
            if email in auth_index:
                indexed_path, auth = auth_index[email]
                auth_path = str(indexed_path)
            records.append(
                AccountDraft(
                    email=email,
                    password=password,
                    sso_token=sso,
                    access_token=str(auth.get("access_token") or ""),
                    refresh_token=str(auth.get("refresh_token") or ""),
                    token_expires_at=str(auth.get("expired") or ""),
                    auth_file=auth_path,
                    source=source,
                    source_modified_at=(
                        source_modified_at
                        or datetime.now(tz=timezone.utc)
                        .replace(microsecond=0)
                        .isoformat()
                        .replace("+00:00", "Z")
                    ),
                )
            )
        return records

    def import_records(
        self,
        account_files: Optional[Sequence[Path]] = None,
        extra_auth_dirs: Sequence[Path] = (),
    ) -> List[AccountDraft]:
        files = list(account_files) if account_files is not None else self.discover_account_files()
        auth_index = self.build_auth_index(extra_auth_dirs)
        by_email: Dict[str, AccountDraft] = {}
        for path in files:
            for record in self.parse_account_file(Path(path), auth_index):
                previous = by_email.get(record.email)
                if previous is None:
                    by_email[record.email] = record
                    continue
                by_email[record.email] = AccountDraft(
                    email=record.email,
                    password=record.password or previous.password,
                    sso_token=record.sso_token or previous.sso_token,
                    access_token=record.access_token or previous.access_token,
                    refresh_token=record.refresh_token or previous.refresh_token,
                    token_expires_at=record.token_expires_at or previous.token_expires_at,
                    auth_file=record.auth_file or previous.auth_file,
                    source=record.source or previous.source,
                    source_modified_at=(
                        record.source_modified_at
                        if record.sso_token
                        else previous.source_modified_at
                    ),
                )
        return list(by_email.values())

    def diagnostics(self, python_executable: str) -> List[Tuple[bool, str]]:
        checks: List[Tuple[bool, str]] = []
        try:
            self.validate()
            checks.append((True, "内置注册运行时完整"))
        except ReferenceProjectError as exc:
            checks.append((False, str(exc)))
            return checks
        try:
            version = subprocess.run(
                [
                    python_executable,
                    "-c",
                    "import sys; print('%s.%s.%s' % sys.version_info[:3])",
                ],
                cwd=str(self.work_dir),
                env=self.environment(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=10,
                check=False,
            )
            version_text = version.stdout.strip()
            compatible = version.returncode == 0 and _is_supported_python(version_text)
            checks.append(
                (
                    compatible,
                    "内置运行 Python: %s（要求 3.13.x）" % (version_text or "未知"),
                )
            )
        except (OSError, subprocess.SubprocessError) as exc:
            checks.append((False, "内置运行 Python 无法执行: %s" % exc))
            return checks
        try:
            imports = subprocess.run(
                [
                    python_executable,
                    "-c",
                    "import grok_register.cli, DrissionPage, curl_cffi, requests; print('依赖完整')",
                ],
                cwd=str(self.work_dir),
                env=self.environment(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=20,
                check=False,
            )
            message = imports.stdout.strip().splitlines()[-1] if imports.stdout.strip() else "依赖检查无输出"
            checks.append((imports.returncode == 0, "内置注册依赖: %s" % message))
        except (OSError, subprocess.SubprocessError) as exc:
            checks.append((False, "注册依赖检查失败: %s" % exc))
        try:
            config = self.load_registration_config()
            provider = str(config.get("email_provider") or "cloudflare")
            base = str(config.get("cloudflare_api_base") or "").strip()
            domain = str(config.get("defaultDomains") or "").strip()
            configured = provider != "cloudflare" or bool(base and domain and "example.com" not in domain)
            checks.append((configured, "邮箱配置: provider=%s, domain=%s" % (provider, domain or "未填写")))
        except ReferenceProjectError as exc:
            checks.append((False, str(exc)))
        return checks


class RegistrationRunner:
    def __init__(self, project: ReferenceProject, python_executable: str):
        self.project = project
        self.python_executable = python_executable
        self._lock = threading.Lock()
        self._process: Optional[subprocess.Popen] = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def cancel(self) -> bool:
        with self._lock:
            process = self._process
        if process is None or process.poll() is not None:
            return False
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                process.terminate()
        else:
            process.terminate()
        return True

    def run(self, request: RegistrationRequest, log: Optional[LogCallback] = None) -> RegistrationResult:
        log = log or (lambda _: None)
        self.project.validate()
        ensure_data_dirs()
        request = RegistrationRequest(
            count=max(1, int(request.count)),
            threads=max(1, min(int(request.threads), 10)),
            mint_workers=max(0, min(int(request.mint_workers), 10)),
        )
        run_name = "manager-%s-%s" % (
            datetime.now().strftime("%Y%m%d-%H%M%S"),
            uuid.uuid4().hex[:6],
        )
        run_dir = self.project.output_dir / run_name
        run_dir.mkdir(parents=True, exist_ok=False)
        accounts_file = run_dir / "accounts.txt"
        command = [
            self.python_executable,
            "-u",
            "-m",
            "grok_register.cli",
            "--count",
            str(request.count),
            "--threads",
            str(request.threads),
            "--mint-workers",
            str(request.mint_workers),
            "--accounts-file",
            str(accounts_file),
        ]
        env = self.project.environment()
        env["PYTHONUNBUFFERED"] = "1"
        batch_dir = ""
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise ReferenceProjectError("已有注册任务正在运行")
            try:
                self._process = subprocess.Popen(
                    command,
                    cwd=str(self.project.work_dir),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=(os.name != "nt"),
                )
            except OSError as exc:
                self._process = None
                return RegistrationResult(
                    ok=False,
                    return_code=127,
                    accounts_file=str(accounts_file),
                    batch_dir="",
                    error="注册进程无法启动: %s" % exc,
                )
            process = self._process
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    text = line.rstrip("\r\n")
                    log(text)
                    matched = re.search(r"本次批次目录\s*=\s*(.+)$", text)
                    if matched:
                        batch_dir = matched.group(1).strip()
            return_code = process.wait()
        finally:
            if process.stdout is not None:
                process.stdout.close()
            with self._lock:
                self._process = None
        if not batch_dir and self.project.output_dir.is_dir():
            directories = [path for path in self.project.output_dir.glob("out_*") if path.is_dir()]
            if directories:
                batch_dir = str(max(directories, key=lambda item: item.stat().st_mtime))
        ok = return_code == 0 and accounts_file.is_file()
        error = "" if ok else "注册任务退出码 %s" % return_code
        return RegistrationResult(
            ok=ok,
            return_code=return_code,
            accounts_file=str(accounts_file),
            batch_dir=batch_dir,
            error=error,
        )
