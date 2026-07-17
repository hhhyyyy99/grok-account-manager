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

from .models import Account, AccountStatus, LoginResult
from .paths import (
    JOBS_DIR,
    MANAGED_AUTH_DIR,
    ensure_data_dirs,
    write_private_text_atomic,
)
from .reference import ReferenceProject
from .store import AccountStore


LogCallback = Callable[[str], None]
ProgressCallback = Callable[[LoginResult, int, int], None]


@dataclass(frozen=True)
class LoginSettings:
    workers: int = 2
    timeout_seconds: int = 300
    proxy: str = ""
    headless: bool = False
    base_url: str = "https://cli-chat-proxy.grok.com/v1"
    probe_after_login: bool = False


class BatchLoginService:
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
        for path in JOBS_DIR.glob("login-*/input.json"):
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

    def login_accounts(
        self,
        account_ids: Iterable[int],
        settings: LoginSettings,
        log: Optional[LogCallback] = None,
        progress: Optional[ProgressCallback] = None,
    ) -> List[LoginResult]:
        log = log or (lambda _: None)
        accounts = self.store.get_many(list(account_ids))
        if not accounts:
            return []
        ready = [account for account in accounts if account.has_login_credentials]
        missing = [account for account in accounts if not account.has_login_credentials]
        results: List[LoginResult] = []
        completed = 0
        total = len(accounts)
        for account in missing:
            result = LoginResult(account.id, account.email, False, "缺少邮箱或密码，无法登录")
            results.append(result)
            self.store.set_status([account.id], AccountStatus.INVALID.value, result.detail)
            completed += 1
            if progress:
                progress(result, completed, total)
        if not ready:
            return results

        self.project.validate()
        ensure_data_dirs()
        self.store.set_status(
            [account.id for account in ready],
            AccountStatus.LOGGING_IN.value,
            "正在通过参考项目登录并重新获取 token",
        )
        run_dir = JOBS_DIR / (
            "login-%s-%s" % (datetime.now().strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6])
        )
        run_dir.mkdir(parents=True, exist_ok=False)
        input_file = run_dir / "input.json"
        document = {
            "settings": {
                "workers": max(1, min(int(settings.workers), 10)),
                "timeout_seconds": max(60, int(settings.timeout_seconds)),
                "proxy": settings.proxy,
                "headless": bool(settings.headless),
                "base_url": settings.base_url,
                "probe": bool(settings.probe_after_login),
                "reuse_browser": True,
                "recycle_every": 10,
                "default_auth_dir": str(MANAGED_AUTH_DIR),
            },
            "accounts": [self._worker_account(account) for account in ready],
        }
        write_private_text_atomic(input_file, json.dumps(document, ensure_ascii=False))
        process: Optional[subprocess.Popen] = None
        worker_script = Path(__file__).with_name("reference_worker.py")
        command = [
            self.python_executable,
            str(worker_script),
            "batch-login",
            "--reference-path",
            str(self.project.root),
            "--input",
            str(input_file),
        ]
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        parsed_ids = set()
        try:
            with self._lock:
                if self._process is not None and self._process.poll() is None:
                    raise RuntimeError("已有批量登录任务正在运行")
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
                log("批量登录工作进程异常退出: %s" % return_code)
        except OSError as exc:
            log("批量登录进程无法启动: %s" % exc)
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

        for account in ready:
            if account.id in parsed_ids:
                continue
            result = LoginResult(account.id, account.email, False, "登录进程未返回该账号结果")
            results.append(result)
            self.store.set_status([account.id], AccountStatus.ERROR.value, result.detail)
            completed += 1
            if progress:
                progress(result, completed, total)
        results.sort(key=lambda item: item.account_id)
        return results

    @staticmethod
    def _worker_account(account: Account) -> Dict[str, Any]:
        auth_dir = str(MANAGED_AUTH_DIR)
        if account.auth_file:
            auth_dir = str(Path(account.auth_file).expanduser().parent)
        return {
            "id": account.id,
            "email": account.email,
            "password": account.password,
            "auth_dir": auth_dir,
        }

    @staticmethod
    def _handle_log(payload: str, log: LogCallback) -> None:
        try:
            value = json.loads(payload)
            email = str(value.get("email") or "")
            message = str(value.get("message") or value.get("error") or value)
            log("[%s] %s" % (email, message) if email else message)
        except (json.JSONDecodeError, AttributeError):
            log(payload)

    def _handle_result(self, payload: str) -> LoginResult:
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            return LoginResult(0, "", False, "登录结果 JSON 无效: %s" % exc)
        account_id = int(value.get("id") or 0)
        email = str(value.get("email") or "")
        ok = bool(value.get("ok"))
        auth_file = str(value.get("path") or "")
        sso_token = str(value.get("sso_token") or "").strip()
        detail = str(value.get("error") or ("批量登录成功" if ok else "批量登录失败"))
        if ok:
            try:
                account = self.store.get(account_id)
                if account is None:
                    raise ValueError("登录结果对应的账号不存在")
                if account.email.casefold() != email.strip().casefold():
                    raise ValueError("登录结果邮箱与账号不匹配")
                auth = json.loads(Path(auth_file).read_text(encoding="utf-8-sig"))
                auth_email = str(auth.get("email") or "").strip()
                if auth_email and account.email.casefold() != auth_email.casefold():
                    raise ValueError("凭据文件邮箱与账号不匹配")
                access_token = str(auth.get("access_token") or "")
                refresh_token = str(auth.get("refresh_token") or "")
                if not access_token or not refresh_token:
                    raise ValueError("凭据文件缺少 access_token/refresh_token")
                self.store.apply_login_credentials(
                    account_id,
                    access_token,
                    refresh_token,
                    str(auth.get("expired") or ""),
                    auth_file,
                    detail="SSO 与 CPA 凭据已刷新" if sso_token else "CPA 凭据已刷新",
                    sso_token=sso_token,
                )
            except (OSError, json.JSONDecodeError, ValueError, AttributeError) as exc:
                ok = False
                detail = "登录成功但凭据回写失败: %s" % exc
            if ok and not sso_token:
                ok = False
                detail = "CPA 凭据已刷新，但浏览器未返回新的 SSO cookie"
        if not ok and account_id:
            self.store.set_status([account_id], AccountStatus.ERROR.value, detail)
        return LoginResult(account_id, email, ok, detail, auth_file)
