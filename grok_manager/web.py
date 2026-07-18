from __future__ import annotations

import json
import secrets
import socket
import threading
import traceback
import uuid
import webbrowser
from dataclasses import asdict, fields
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from .config import ManagerConfig
from .exports import AccountExport, AccountExporter
from .models import Account, AccountStatus, status_label
from .reference import RegistrationRequest
from .service import GrokManager


ASSET_DIR = Path(__file__).resolve().parent / "web_assets"
TERMINAL_STATES = {"succeeded", "failed", "cancelled"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_visible(value: Any) -> str:
    return str(value or "").replace("—", "-").replace("–", "-")


class TaskRecord:
    def __init__(self, kind: str, label: str, exclusive_group: str = ""):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.label = label
        self.exclusive_group = exclusive_group
        self.state = "queued"
        self.created_at = now_iso()
        self.started_at = ""
        self.finished_at = ""
        self.current = 0
        self.total = 0
        self.message = "等待执行"
        self.error = ""
        self.result: Dict[str, Any] = {}
        self.cancel_requested = False
        self._logs: List[Dict[str, Any]] = []
        self._sequence = 0
        self._lock = threading.RLock()

    def log(self, message: str) -> None:
        text = safe_visible(message).strip()
        if not text:
            return
        with self._lock:
            self._sequence += 1
            self._logs.append(
                {
                    "seq": self._sequence,
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "message": text,
                }
            )
            if len(self._logs) > 800:
                del self._logs[: len(self._logs) - 800]

    def progress(self, current: int, total: int, message: str) -> None:
        with self._lock:
            self.current = max(0, int(current))
            self.total = max(0, int(total))
            self.message = safe_visible(message)

    def serialize(self, include_logs: bool = True) -> Dict[str, Any]:
        with self._lock:
            value = {
                "id": self.id,
                "kind": self.kind,
                "label": self.label,
                "state": self.state,
                "createdAt": self.created_at,
                "startedAt": self.started_at,
                "finishedAt": self.finished_at,
                "current": self.current,
                "total": self.total,
                "message": self.message,
                "error": self.error,
                "result": self.result,
                "cancelRequested": self.cancel_requested,
            }
            if include_logs:
                value["logs"] = list(self._logs)
            return value


TaskWorker = Callable[[TaskRecord], Dict[str, Any]]


class TaskRegistry:
    def __init__(self):
        self._tasks: Dict[str, TaskRecord] = {}
        self._order: List[str] = []
        self._lock = threading.RLock()

    def start(
        self,
        kind: str,
        label: str,
        worker: TaskWorker,
        exclusive_group: str = "",
    ) -> TaskRecord:
        with self._lock:
            if exclusive_group:
                for task in self._tasks.values():
                    if task.exclusive_group == exclusive_group and task.state not in TERMINAL_STATES:
                        raise RuntimeError("已有同类任务正在运行: %s" % task.label)
            task = TaskRecord(kind, label, exclusive_group)
            self._tasks[task.id] = task
            self._order.append(task.id)
            if len(self._order) > 200:
                removable = [
                    task_id
                    for task_id in self._order
                    if self._tasks[task_id].state in TERMINAL_STATES
                ]
                for task_id in removable[: max(0, len(self._order) - 200)]:
                    self._order.remove(task_id)
                    self._tasks.pop(task_id, None)

        def run() -> None:
            task.state = "running"
            task.started_at = now_iso()
            task.message = "正在执行"
            try:
                result = worker(task) or {}
                task.result = result
                if task.cancel_requested:
                    task.state = "cancelled"
                    task.message = "任务已取消"
                else:
                    task.state = "succeeded"
                    task.message = safe_visible(result.get("message") or "任务完成")
            except Exception as exc:
                if task.cancel_requested:
                    task.state = "cancelled"
                    task.message = "任务已取消"
                    task.log("任务在取消后结束")
                else:
                    task.state = "failed"
                    task.error = safe_visible(exc)
                    task.message = "任务失败"
                    task.log("错误: %s" % exc)
                    traceback.print_exc()
            finally:
                task.finished_at = now_iso()

        threading.Thread(target=run, name="web-task-%s" % task.id, daemon=True).start()
        return task

    def get(self, task_id: str) -> Optional[TaskRecord]:
        with self._lock:
            return self._tasks.get(task_id)

    def latest(self, limit: int = 12) -> List[TaskRecord]:
        with self._lock:
            ids = list(reversed(self._order[-max(1, int(limit)) :]))
            return [self._tasks[task_id] for task_id in ids]

    def has_running(self) -> bool:
        with self._lock:
            return any(task.state not in TERMINAL_STATES for task in self._tasks.values())


class GrokWebApplication:
    def __init__(self, manager: Optional[GrokManager] = None):
        self.manager = manager or GrokManager()
        self.tasks = TaskRegistry()
        self.csrf_token = secrets.token_urlsafe(32)
        self._server: Optional[ThreadingHTTPServer] = None
        if self.manager.config.auto_import_on_start:
            try:
                self.start_import({})
            except RuntimeError:
                pass

    @staticmethod
    def account_json(account: Account) -> Dict[str, Any]:
        cpa_status = (
            AccountStatus.MISSING_CPA.value
            if account.missing_cpa_credentials
            else account.cpa_status
        )
        return {
            "id": account.id,
            "email": safe_visible(account.email),
            "status": account.status,
            "statusLabel": account.status_label,
            "detail": safe_visible(account.status_detail),
            "expiresAt": safe_visible(account.token_expires_at),
            "lastCheckedAt": safe_visible(account.last_checked_at),
            "lastLoginAt": safe_visible(account.last_login_at),
            "createdAt": safe_visible(account.created_at),
            "updatedAt": safe_visible(account.updated_at),
            "hasPassword": bool(account.password),
            "hasSso": bool(account.sso_token),
            "hasAccessToken": bool(account.access_token),
            "ssoStatus": account.sso_status,
            "ssoStatusLabel": account.sso_status_label,
            "ssoDetail": safe_visible(account.sso_detail),
            "ssoExpiresAt": safe_visible(account.sso_expires_at),
            "cpaStatus": cpa_status,
            "cpaStatusLabel": status_label(cpa_status),
            "cpaDetail": safe_visible(account.cpa_detail),
            "missingCpa": account.missing_cpa_credentials,
            "source": safe_visible(account.source),
        }

    def state_json(self, query: Dict[str, List[str]]) -> Dict[str, Any]:
        search = (query.get("search") or [""])[0]
        status = (query.get("status") or [""])[0]
        try:
            requested_page = max(1, int((query.get("page") or ["1"])[0]))
        except (TypeError, ValueError):
            requested_page = 1
        try:
            page_size = max(1, min(200, int((query.get("page_size") or ["50"])[0])))
        except (TypeError, ValueError):
            page_size = 50
        total = self.manager.store.count_accounts(search=search, status=status)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(requested_page, total_pages)
        accounts = self.manager.store.list_accounts(
            search=search,
            status=status,
            limit=page_size,
            offset=(page - 1) * page_size,
        )
        return {
            "accounts": [self.account_json(account) for account in accounts],
            "pagination": {
                "page": page,
                "pageSize": page_size,
                "total": total,
                "totalPages": total_pages,
            },
            "stats": self.manager.store.stats(),
            "tasks": [task.serialize(include_logs=False) for task in self.tasks.latest()],
        }

    def selection_json(self, query: Dict[str, List[str]]) -> Dict[str, Any]:
        search = (query.get("search") or [""])[0]
        status = (query.get("status") or [""])[0]
        total = self.manager.store.count_accounts(search=search, status=status)
        if total > 10000:
            raise ValueError("单次最多选择 10000 个账号，请先缩小筛选范围")
        accounts = self.manager.store.list_accounts(search=search, status=status)
        return {
            "ids": [account.id for account in accounts],
            "total": total,
        }

    def config_json(self) -> Dict[str, Any]:
        reference_config: Dict[str, Any] = {}
        reference_error = ""
        try:
            reference_config = self.manager.reference.load_registration_config()
        except Exception as exc:
            reference_error = safe_visible(exc)
        return {
            "manager": asdict(self.manager.config),
            "registration": reference_config,
            "registrationError": reference_error,
            "runtimePython": self.manager.python_executable,
            "runtimeRoot": str(self.manager.reference.root),
        }

    @staticmethod
    def _ids(payload: Dict[str, Any]) -> List[int]:
        raw = payload.get("ids") or []
        if not isinstance(raw, list):
            raise ValueError("ids 必须是数组")
        ids = []
        for value in raw:
            account_id = int(value)
            if account_id > 0 and account_id not in ids:
                ids.append(account_id)
        if len(ids) > 10000:
            raise ValueError("单次任务账号数量过多")
        return ids

    def export_accounts(self, payload: Dict[str, Any]) -> AccountExport:
        export_format = str(payload.get("format") or "").strip().lower()
        ids = self._ids(payload)
        if ids:
            accounts = self.manager.store.get_many(ids)
        else:
            search = str(payload.get("search") or "")
            status = str(payload.get("status") or "")
            accounts = self.manager.store.list_accounts(
                search=search,
                status=status,
                limit=10001,
            )
            if len(accounts) > 10000:
                raise ValueError("单次最多导出 10000 个账号，请先缩小筛选范围")
        if not accounts:
            raise ValueError("没有符合条件的账号可导出")
        try:
            registration_config = self.manager.reference.load_registration_config()
        except Exception:
            registration_config = {}
        return AccountExporter(registration_config).export(accounts, export_format)

    def start_import(self, payload: Dict[str, Any]) -> TaskRecord:
        content = payload.get("content")
        filename = safe_visible(payload.get("filename") or "browser-import.txt")

        def worker(task: TaskRecord) -> Dict[str, Any]:
            task.log("开始导入账号产物")
            if content is None:
                accounts = self.manager.import_reference_accounts()
            else:
                if not isinstance(content, str) or len(content.encode("utf-8")) > 5 * 1024 * 1024:
                    raise ValueError("导入文件必须是 5MB 以内的文本")
                accounts = self.manager.import_account_text(content, source=filename)
            task.progress(len(accounts), len(accounts), "已导入 %s 个账号" % len(accounts))
            task.log("导入完成，共 %s 个账号" % len(accounts))
            return {"count": len(accounts), "message": "导入完成"}

        return self.tasks.start("import", "导入账号", worker, exclusive_group="database-import")

    def start_inspection(self, payload: Dict[str, Any]) -> TaskRecord:
        ids = self._ids(payload)
        if payload.get("all"):
            ids = [account.id for account in self.manager.store.list_accounts()]
        if not ids:
            raise ValueError("没有待巡检账号")
        live = bool(payload.get("live", self.manager.config.live_probe))

        def worker(task: TaskRecord) -> Dict[str, Any]:
            task.log(
                "开始%s巡检 %s 个账号的 SSO 与 CPA token"
                % ("在线" if live else "本地", len(ids))
            )
            task.progress(0, len(ids), "等待巡检 worker")

            def progress(result, completed, total):
                task.progress(completed, total, "%s: %s" % (result.account_id, result.detail))
                task.log("#%s %s: %s" % (result.account_id, result.status, result.detail))

            results = self.manager.inspect_accounts(
                ids,
                live=live,
                progress=progress,
                cancelled=lambda: task.cancel_requested,
            )
            attention = sum(
                1
                for result in results
                if result.status
                in (
                    AccountStatus.EXPIRED.value,
                    AccountStatus.INVALID.value,
                    AccountStatus.NEEDS_LOGIN.value,
                    AccountStatus.ERROR.value,
                    AccountStatus.UNKNOWN.value,
                )
            )
            return {
                "count": len(results),
                "attention": attention,
                "message": "巡检完成，需要处理 %s 个" % attention,
            }

        return self.tasks.start("inspect", "账号巡检", worker, exclusive_group="inspection")

    def start_login(self, payload: Dict[str, Any]) -> TaskRecord:
        ids = self._ids(payload)
        if payload.get("candidates"):
            ids = self.manager.relogin_candidate_ids()
        if not ids:
            raise ValueError("没有待登录账号")

        def worker(task: TaskRecord) -> Dict[str, Any]:
            task.log("开始批量登录 %s 个账号" % len(ids))

            def progress(result, completed, total):
                task.progress(completed, total, "%s: %s" % (result.email, result.detail))

            results = self.manager.batch_login(ids, log=task.log, progress=progress)
            succeeded = sum(1 for result in results if result.ok)
            failed = len(results) - succeeded
            return {
                "count": len(results),
                "succeeded": succeeded,
                "failed": failed,
                "message": "登录完成，成功 %s，失败 %s" % (succeeded, failed),
            }

        return self.tasks.start("login", "批量登录", worker, exclusive_group="browser-automation")

    def start_password_reset(self, payload: Dict[str, Any]) -> TaskRecord:
        ids = self._ids(payload)
        if not ids:
            raise ValueError("没有待重置密码的账号")

        def worker(task: TaskRecord) -> Dict[str, Any]:
            task.log("开始重置 %s 个账号的密码" % len(ids))

            def reset_progress(result, completed, total):
                task.progress(completed, total * 2, "%s: %s" % (result.email, result.detail))
                task.log("#%s %s: %s" % (result.account_id, result.email, result.detail))

            reset_results = self.manager.reset_passwords(
                ids,
                log=task.log,
                progress=reset_progress,
            )
            reset_ids = [result.account_id for result in reset_results if result.ok]
            login_results = []
            if reset_ids:
                task.log("密码重置完成 %s 个，开始自动重新登录" % len(reset_ids))

                def login_progress(result, completed, total):
                    task.progress(
                        len(ids) + completed,
                        len(ids) * 2,
                        "%s: %s" % (result.email, result.detail),
                    )

                login_results = self.manager.batch_login(
                    reset_ids,
                    log=task.log,
                    progress=login_progress,
                )
            reset_succeeded = sum(1 for result in reset_results if result.ok)
            login_succeeded = sum(1 for result in login_results if result.ok)
            return {
                "resetCount": len(reset_results),
                "resetSucceeded": reset_succeeded,
                "loginCount": len(login_results),
                "loginSucceeded": login_succeeded,
                "message": "密码重置 %s 个，自动登录成功 %s 个"
                % (reset_succeeded, login_succeeded),
            }

        return self.tasks.start("reset-password", "重置密码并重新登录", worker, exclusive_group="browser-automation")

    def start_registration(self, payload: Dict[str, Any]) -> TaskRecord:
        request = RegistrationRequest(
            count=max(1, int(payload.get("count") or self.manager.config.register_count)),
            threads=max(1, int(payload.get("threads") or self.manager.config.register_threads)),
            mint_workers=max(0, int(payload.get("mintWorkers", self.manager.config.mint_workers))),
        )

        def worker(task: TaskRecord) -> Dict[str, Any]:
            task.log(
                "开始注册，数量 %s，注册并发 %s，Mint 并发 %s"
                % (request.count, request.threads, request.mint_workers)
            )
            result = self.manager.run_registration(request, log=task.log)
            if not result.ok:
                raise RuntimeError(result.error or "注册任务未成功完成")
            return {
                "imported": result.imported_count,
                "accountsFile": safe_visible(result.accounts_file),
                "message": "注册完成，导入 %s 个账号" % result.imported_count,
            }

        return self.tasks.start("register", "批量注册", worker, exclusive_group="browser-automation")

    def start_diagnostics(self) -> TaskRecord:
        def worker(task: TaskRecord) -> Dict[str, Any]:
            task.log("开始检查内置注册环境")
            checks = []
            for ok, message in self.manager.diagnostics():
                checks.append({"ok": bool(ok), "message": safe_visible(message)})
                task.log("%s %s" % ("通过" if ok else "失败", message))
            passed = sum(1 for item in checks if item["ok"])
            return {
                "checks": checks,
                "passed": passed,
                "total": len(checks),
                "message": "环境检查完成，%s/%s 通过" % (passed, len(checks)),
            }

        return self.tasks.start("diagnostics", "环境检查", worker, exclusive_group="diagnostics")

    def cancel_task(self, task_id: str) -> TaskRecord:
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError("任务不存在")
        if task.state in TERMINAL_STATES:
            return task
        if task.kind not in ("register", "login", "reset-password", "inspect"):
            raise RuntimeError("该任务不支持中途取消")
        task.cancel_requested = True
        if task.kind == "register":
            self.manager.registration.cancel()
        elif task.kind == "login":
            self.manager.login.cancel()
        elif task.kind == "reset-password":
            self.manager.password_reset.cancel()
        elif task.kind == "inspect":
            task.message = "正在停止巡检"
        task.log("已请求取消任务")
        return task

    def save_manager_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self.tasks.has_running():
            raise RuntimeError("有任务运行时不能修改管理端配置")
        known = {item.name for item in fields(ManagerConfig)}
        values = asdict(self.manager.config)
        values.update({key: value for key, value in payload.items() if key in known})
        config = ManagerConfig(**values).normalized()
        self.manager.save_manager_config(config)
        return self.config_json()

    def save_reference_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self.tasks.has_running():
            raise RuntimeError("有任务运行时不能修改注册配置")
        self.manager.reference.save_registration_config(payload)
        return self.config_json()

    def serve(self, host: str = "127.0.0.1", port: int = 8787, open_browser: bool = True) -> None:
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("为保护账号凭据，管理端只允许绑定本机回环地址")
        application = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "GrokManager/0.1"
            sys_version = ""

            def log_message(self, format: str, *args: Any) -> None:
                return None

            def _headers(
                self,
                content_type: str,
                content_length: int,
                status: int = 200,
                extra_headers: Optional[Dict[str, str]] = None,
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(content_length))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                    "base-uri 'none'; frame-ancestors 'none'",
                )
                for name, value in (extra_headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()

            def _bytes(
                self,
                body: bytes,
                content_type: str,
                status: int = 200,
                extra_headers: Optional[Dict[str, str]] = None,
            ) -> None:
                self._headers(content_type, len(body), status, extra_headers)
                self.wfile.write(body)

            def _json(self, value: Any, status: int = 200) -> None:
                body = json.dumps(value, ensure_ascii=False).encode("utf-8")
                self._bytes(body, "application/json; charset=utf-8", status)

            def _error(self, status: int, message: str) -> None:
                self._json({"error": safe_visible(message)}, status)

            def _read_json(self) -> Dict[str, Any]:
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/json":
                    raise ValueError("请求必须使用 application/json")
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError as exc:
                    raise ValueError("Content-Length 无效") from exc
                if length < 0 or length > 6 * 1024 * 1024:
                    raise ValueError("请求体过大")
                raw = self.rfile.read(length)
                value = json.loads(raw.decode("utf-8")) if raw else {}
                if not isinstance(value, dict):
                    raise ValueError("请求 JSON 必须是对象")
                return value

            def _authorized(self) -> bool:
                return secrets.compare_digest(
                    self.headers.get("X-Grok-Manager-Token", ""), application.csrf_token
                )

            def _trusted_host(self) -> bool:
                host = self.headers.get("Host", "").strip().lower()
                if host.startswith("["):
                    closing = host.find("]")
                    if closing < 0 or host[: closing + 1] != "[::1]":
                        return False
                    suffix = host[closing + 1 :]
                    return not suffix or (suffix.startswith(":") and suffix[1:].isdigit())
                hostname, separator, port = host.rpartition(":")
                if not separator:
                    hostname = host
                elif not port.isdigit():
                    return False
                return hostname in ("127.0.0.1", "localhost")

            def _allow_request(self) -> bool:
                if self._trusted_host():
                    return True
                self._error(403, "仅允许通过本机回环地址访问")
                return False

            def do_GET(self) -> None:
                if not self._allow_request():
                    return
                parsed = urlparse(self.path)
                try:
                    if parsed.path in ("/", "/index.html"):
                        template = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
                        body = template.replace("__GROK_MANAGER_TOKEN__", application.csrf_token).encode("utf-8")
                        self._bytes(body, "text/html; charset=utf-8")
                        return
                    if parsed.path == "/assets/app.css":
                        self._bytes((ASSET_DIR / "app.css").read_bytes(), "text/css; charset=utf-8")
                        return
                    if parsed.path == "/assets/app.js":
                        self._bytes((ASSET_DIR / "app.js").read_bytes(), "text/javascript; charset=utf-8")
                        return
                    if parsed.path == "/api/health":
                        self._json({"ok": True})
                        return
                    if parsed.path == "/api/state":
                        if not self._authorized():
                            self._error(403, "请求令牌无效")
                            return
                        self._json(application.state_json(parse_qs(parsed.query)))
                        return
                    if parsed.path == "/api/accounts/selection":
                        if not self._authorized():
                            self._error(403, "请求令牌无效")
                            return
                        self._json(application.selection_json(parse_qs(parsed.query)))
                        return
                    if parsed.path == "/api/config":
                        if not self._authorized():
                            self._error(403, "请求令牌无效")
                            return
                        self._json(application.config_json())
                        return
                    if parsed.path == "/api/tasks":
                        if not self._authorized():
                            self._error(403, "请求令牌无效")
                            return
                        self._json({"tasks": [task.serialize() for task in application.tasks.latest()]})
                        return
                    if parsed.path.startswith("/api/tasks/"):
                        if not self._authorized():
                            self._error(403, "请求令牌无效")
                            return
                        task_id = parsed.path.rsplit("/", 1)[-1]
                        task = application.tasks.get(task_id)
                        if task is None:
                            self._error(404, "任务不存在")
                        else:
                            self._json(task.serialize())
                        return
                    self._error(404, "接口不存在")
                except ValueError as exc:
                    self._error(400, str(exc))
                except Exception as exc:
                    self._error(500, str(exc))

            def do_POST(self) -> None:
                if not self._allow_request():
                    return
                if not self._authorized():
                    self._error(403, "请求令牌无效")
                    return
                parsed = urlparse(self.path)
                try:
                    payload = self._read_json()
                    if parsed.path == "/api/import":
                        self._json({"task": application.start_import(payload).serialize(False)}, 202)
                    elif parsed.path == "/api/inspect":
                        self._json({"task": application.start_inspection(payload).serialize(False)}, 202)
                    elif parsed.path == "/api/reset-password":
                        self._json({"task": application.start_password_reset(payload).serialize(False)}, 202)
                    elif parsed.path == "/api/login":
                        self._json({"task": application.start_login(payload).serialize(False)}, 202)
                    elif parsed.path == "/api/register":
                        self._json({"task": application.start_registration(payload).serialize(False)}, 202)
                    elif parsed.path == "/api/diagnostics":
                        self._json({"task": application.start_diagnostics().serialize(False)}, 202)
                    elif parsed.path == "/api/config/manager":
                        self._json(application.save_manager_config(payload))
                    elif parsed.path in ("/api/config/registration", "/api/config/reference"):
                        self._json(application.save_reference_config(payload))
                    elif parsed.path == "/api/accounts/delete":
                        ids = application._ids(payload)
                        self._json({"deleted": application.manager.store.delete(ids)})
                    elif parsed.path == "/api/accounts/export":
                        exported = application.export_accounts(payload)
                        self._bytes(
                            exported.body,
                            exported.content_type,
                            extra_headers={
                                "Content-Disposition": (
                                    'attachment; filename="%s"' % exported.filename
                                ),
                                "X-Exported-Count": str(exported.exported_count),
                                "X-Skipped-Count": str(exported.skipped_count),
                            },
                        )
                    elif parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/cancel"):
                        task_id = parsed.path.split("/")[-2]
                        self._json({"task": application.cancel_task(task_id).serialize(False)})
                    else:
                        self._error(404, "接口不存在")
                except KeyError as exc:
                    self._error(404, str(exc))
                except (ValueError, RuntimeError) as exc:
                    self._error(400, str(exc))
                except Exception as exc:
                    traceback.print_exc()
                    self._error(500, str(exc))

        server_class = ThreadingHTTPServer
        if host == "::1":
            class IPv6ThreadingHTTPServer(ThreadingHTTPServer):
                address_family = socket.AF_INET6

            server_class = IPv6ThreadingHTTPServer
        server = server_class((host, int(port)), Handler)
        server.daemon_threads = True
        self._server = server
        display_host = "[%s]" % host if host == "::1" else host
        url = "http://%s:%s" % (display_host, server.server_address[1])
        print("Grok Account Manager: %s" % url, flush=True)
        if open_browser:
            threading.Timer(0.4, lambda: webbrowser.open(url)).start()
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
