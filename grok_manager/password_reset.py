from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .models import Account, AccountStatus, PasswordResetResult
from .paths import JOBS_DIR, ensure_data_dirs, write_private_text_atomic
from .reference import ReferenceProject
from .store import AccountStore

LogCallback = Callable[[str], None]
ProgressCallback = Callable[[PasswordResetResult, int, int], None]


@dataclass(frozen=True)
class PasswordResetSettings:
    workers: int = 2
    timeout_seconds: int = 300
    proxy: str = ""
    headless: bool = False


class BatchPasswordResetService:
    def __init__(
        self,
        store: AccountStore,
        project: ReferenceProject,
        python_executable: str,
    ):
        self.store = store
        self.project = project
        self.python_executable = python_executable
        self._lock = threading.Lock()
        self._process: Optional[subprocess.Popen] = None
        self._cleanup_stale_inputs()

    @staticmethod
    def _cleanup_stale_inputs() -> None:
        ensure_data_dirs()
        stale_before = time.time() - 24 * 60 * 60
        for path in JOBS_DIR.glob("password-reset-*/input.json"):
            try:
                if path.stat().st_mtime < stale_before:
                    path.unlink()
            except OSError:
                pass

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

    def reset_accounts(
        self,
        account_ids: Iterable[int],
        settings: PasswordResetSettings,
        log: Optional[LogCallback] = None,
        progress: Optional[ProgressCallback] = None,
    ) -> List[PasswordResetResult]:
        log = log or (lambda _message: None)
        accounts = self.store.get_many(list(account_ids))
        if not accounts:
            return []
        ready: List[tuple[Account, str]] = []
        results: List[PasswordResetResult] = []
        completed = 0
        total = len(accounts)
        for account in accounts:
            credential = self.project.find_mail_credential(account.email, account.source)
            if not credential:
                result = PasswordResetResult(
                    account.id,
                    account.email,
                    False,
                    "找不到该账号的邮箱访问凭据，无法接收重置验证码",
                )
                results.append(result)
                self.store.set_status([account.id], AccountStatus.ERROR.value, result.detail)
                completed += 1
                if progress:
                    progress(result, completed, total)
            else:
                ready.append((account, credential))
        if not ready:
            return results

        self.project.validate()
        ensure_data_dirs()
        run_dir = JOBS_DIR / (
            "password-reset-%s-%s"
            % (datetime.now().strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6])
        )
        run_dir.mkdir(parents=True, exist_ok=False)
        input_file = run_dir / "input.json"
        document = {
            "settings": {
                "workers": max(1, min(int(settings.workers), 10)),
                "timeout_seconds": max(60, int(settings.timeout_seconds)),
                "proxy": settings.proxy,
                "headless": bool(settings.headless),
            },
            "accounts": [
                {
                    "id": account.id,
                    "email": account.email,
                    "mail_credential": credential,
                }
                for account, credential in ready
            ],
        }
        write_private_text_atomic(input_file, json.dumps(document, ensure_ascii=False))
        process: Optional[subprocess.Popen] = None
        worker_script = Path(__file__).with_name("reference_worker.py")
        command = [
            self.python_executable,
            str(worker_script),
            "reset-password",
            "--input",
            str(input_file),
        ]
        env = self.project.environment()
        env["PYTHONUNBUFFERED"] = "1"
        parsed_ids = set()
        try:
            with self._lock:
                if self._process is not None and self._process.poll() is None:
                    raise RuntimeError("已有密码重置任务正在运行")
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
                process = self._process
            if process.stdout is not None:
                for line in process.stdout:
                    text = line.rstrip("\r\n")
                    if text.startswith("GM_LOG "):
                        self._handle_log(text[7:], log)
                    elif text.startswith("GM_RESULT "):
                        result = self._handle_result(text[10:])
                        results.append(result)
                        parsed_ids.add(result.account_id)
                        completed += 1
                        if progress:
                            progress(result, completed, total)
                    elif text.startswith("GM_FATAL "):
                        self._handle_log(text[9:], log)
                    elif text:
                        log(text)
            return_code = process.wait()
            if return_code not in (0, 2):
                log("密码重置工作进程异常退出: %s" % return_code)
        except OSError as exc:
            log("密码重置进程无法启动: %s" % exc)
        finally:
            if process is not None and process.stdout is not None:
                process.stdout.close()
            with self._lock:
                if self._process is process:
                    self._process = None
            try:
                input_file.unlink()
            except OSError:
                pass

        for account, _credential in ready:
            if account.id in parsed_ids:
                continue
            result = PasswordResetResult(
                account.id, account.email, False, "密码重置进程未返回该账号结果"
            )
            results.append(result)
            self.store.set_status([account.id], AccountStatus.ERROR.value, result.detail)
            completed += 1
            if progress:
                progress(result, completed, total)
        results.sort(key=lambda item: item.account_id)
        return results

    def _handle_result(self, payload: str) -> PasswordResetResult:
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            return PasswordResetResult(0, "", False, "密码重置结果 JSON 无效: %s" % exc)
        account_id = int(value.get("id") or 0)
        email = str(value.get("email") or "")
        ok = bool(value.get("ok"))
        detail = str(value.get("error") or ("密码已重置" if ok else "密码重置失败"))
        if ok:
            try:
                account = self.store.get(account_id)
                if account is None:
                    raise ValueError("密码重置结果对应的账号不存在")
                if account.email.casefold() != email.strip().casefold():
                    raise ValueError("密码重置结果邮箱与账号不匹配")
                password = str(value.get("password") or "").strip()
                if not password:
                    raise ValueError("重置结果缺少新密码")
                self.store.apply_password_reset(account_id, password)
                try:
                    path = self.project.persist_account_password(
                        account.email, password, account.source
                    )
                    detail = "密码已重置，已写入 %s" % path.name
                except Exception as exc:
                    detail = "密码已重置，数据库已更新但产物写入失败: %s" % exc
            except (ValueError, OSError) as exc:
                ok = False
                detail = "密码已重置但回写失败: %s" % exc
        if not ok and account_id:
            self.store.set_status([account_id], AccountStatus.ERROR.value, detail)
        return PasswordResetResult(account_id, email, ok, detail)

    @staticmethod
    def _handle_log(payload: str, log: LogCallback) -> None:
        try:
            value = json.loads(payload)
            email = str(value.get("email") or "")
            message = str(value.get("message") or value.get("error") or value)
            log("[%s] %s" % (email, message) if email else message)
        except (json.JSONDecodeError, AttributeError):
            log(payload)
