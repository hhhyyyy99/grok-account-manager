from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .models import AccountDraft
from .paths import JOBS_DIR, ensure_data_dirs, write_private_text_atomic


LogCallback = Callable[[str], None]


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
    """Stable adapter around the grok-register-mint project on disk."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()

    @property
    def entrypoint(self) -> Path:
        return self.root / "register_cli.py"

    @property
    def config_file(self) -> Path:
        return self.root / "config.json"

    @property
    def config_example_file(self) -> Path:
        return self.root / "config.example.json"

    @property
    def output_dir(self) -> Path:
        return self.root / "output"

    def validate(self) -> None:
        missing = []
        for path in (self.entrypoint, self.config_example_file, self.root / "grok_register"):
            if not path.exists():
                missing.append(str(path))
        if missing:
            raise ReferenceProjectError("参考项目不完整，缺少: %s" % ", ".join(missing))

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
                raise ReferenceProjectError("参考项目 config.json 读取失败: %s" % exc) from exc
            if not isinstance(local, dict):
                raise ReferenceProjectError("参考项目 config.json 必须是 JSON 对象")
            base.update(local)
        return base

    def save_registration_config(self, values: Dict[str, Any]) -> Path:
        if not isinstance(values, dict):
            raise ValueError("注册配置必须是 JSON 对象")
        self.validate()
        return write_private_text_atomic(
            self.config_file,
            json.dumps(values, ensure_ascii=False, indent=2) + "\n",
        )

    def discover_account_files(self) -> List[Path]:
        candidates: Dict[str, Path] = {}
        patterns = (
            "output/out_*/accounts*.txt",
            "output/accounts*.txt",
            "accounts*.txt",
        )
        for pattern in patterns:
            for path in self.root.glob(pattern):
                if path.is_file():
                    candidates[str(path.resolve())] = path.resolve()
        return sorted(candidates.values(), key=lambda item: (item.stat().st_mtime, str(item)))

    def discover_auth_files(self, extra_dirs: Sequence[Path] = ()) -> List[Path]:
        candidates: Dict[str, Path] = {}
        for path in self.root.glob("output/**/xai-*.json"):
            if path.is_file():
                candidates[str(path.resolve())] = path.resolve()
        try:
            configured = str(self.load_registration_config().get("cpa_auth_dir") or "").strip()
        except ReferenceProjectError:
            configured = ""
        if configured:
            auth_dir = Path(configured).expanduser()
            if not auth_dir.is_absolute():
                auth_dir = self.root / auth_dir
            for path in auth_dir.glob("xai-*.json"):
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
            checks.append((True, "参考项目结构完整"))
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
                cwd=str(self.root),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=10,
                check=False,
            )
            version_text = version.stdout.strip()
            matched = re.match(r"^(\d+)\.(\d+)\.(\d+)", version_text)
            compatible = bool(
                version.returncode == 0
                and matched
                and (int(matched.group(1)), int(matched.group(2))) == (3, 13)
            )
            checks.append(
                (
                    compatible,
                    "注册 Python: %s（参考项目要求 3.13）" % (version_text or "未知"),
                )
            )
        except (OSError, subprocess.SubprocessError) as exc:
            checks.append((False, "注册 Python 无法执行: %s" % exc))
            return checks
        try:
            imports = subprocess.run(
                [python_executable, "-c", "import DrissionPage, curl_cffi; print('依赖完整')"],
                cwd=str(self.root),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=20,
                check=False,
            )
            message = imports.stdout.strip().splitlines()[-1] if imports.stdout.strip() else "依赖检查无输出"
            checks.append((imports.returncode == 0, "注册环境: %s" % message))
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
        run_name = "register-%s-%s" % (
            datetime.now().strftime("%Y%m%d-%H%M%S"),
            uuid.uuid4().hex[:6],
        )
        run_dir = JOBS_DIR / run_name
        run_dir.mkdir(parents=True, exist_ok=False)
        accounts_file = run_dir / "accounts.txt"
        command = [
            self.python_executable,
            "-u",
            str(self.project.entrypoint),
            "--count",
            str(request.count),
            "--threads",
            str(request.threads),
            "--mint-workers",
            str(request.mint_workers),
            "--accounts-file",
            str(accounts_file),
        ]
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        batch_dir = ""
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise ReferenceProjectError("已有注册任务正在运行")
            try:
                self._process = subprocess.Popen(
                    command,
                    cwd=str(self.project.root),
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
