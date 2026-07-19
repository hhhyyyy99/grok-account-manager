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
from .reference import RegistrationRequest, SENSITIVE_CONFIG_KEYS
from .service import GrokManager


ASSET_DIR = Path(__file__).resolve().parent / "web_assets"
TERMINAL_STATES = {"succeeded", "partial", "failed", "cancelled"}
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", "[::]"})


def _result_success_failure_counts(result: Dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    """Best-effort (succeeded, failed) extraction from a task result payload."""
    if not isinstance(result, dict):
        return None, None
    if result.get("ok") is False:
        return 0, 1

    succeeded: Optional[int] = None
    failed: Optional[int] = None

    raw_succeeded = result.get("succeeded")
    raw_failed = result.get("failed")
    if isinstance(raw_succeeded, (int, float)):
        succeeded = max(0, int(raw_succeeded))
    if isinstance(raw_failed, (int, float)):
        failed = max(0, int(raw_failed))

    raw_failed_count = result.get("failedCount")
    if failed is None and isinstance(raw_failed_count, (int, float)):
        failed = max(0, int(raw_failed_count))

    reset_count = result.get("resetCount")
    reset_succeeded = result.get("resetSucceeded")
    login_count = result.get("loginCount")
    login_succeeded = result.get("loginSucceeded")
    if (
        isinstance(reset_count, (int, float))
        and isinstance(reset_succeeded, (int, float))
    ):
        reset_failed = max(0, int(reset_count) - int(reset_succeeded))
        login_failed = 0
        login_ok = 0
        if isinstance(login_count, (int, float)) and isinstance(login_succeeded, (int, float)):
            login_failed = max(0, int(login_count) - int(login_succeeded))
            login_ok = max(0, int(login_succeeded))
        # Treat a fully successful reset+login path as success units.
        path_succeeded = max(0, int(reset_succeeded) if not login_count else login_ok)
        path_failed = reset_failed + login_failed
        succeeded = path_succeeded if succeeded is None else succeeded
        failed = path_failed if failed is None else failed
    elif (
        isinstance(login_count, (int, float))
        and isinstance(login_succeeded, (int, float))
    ):
        login_failed = max(0, int(login_count) - int(login_succeeded))
        if succeeded is None:
            succeeded = max(0, int(login_succeeded))
        if failed is None:
            failed = login_failed

    return succeeded, failed


def task_result_outcome(result: Dict[str, Any]) -> str:
    """Classify finished batch work: succeeded | partial | failed."""
    succeeded, failed = _result_success_failure_counts(result)
    if succeeded is None and failed is None:
        return "succeeded"
    success_count = int(succeeded or 0)
    failure_count = int(failed or 0)
    if failure_count <= 0:
        return "succeeded"
    if success_count > 0:
        return "partial"
    return "failed"


def task_result_failed(result: Dict[str, Any]) -> bool:
    """True when the task has any account-level failure (partial or total)."""
    return task_result_outcome(result) in {"partial", "failed"}


def summarize_account_results(
    results: List[Any],
    *,
    action_label: str,
    max_failures: int = 200,
) -> Dict[str, Any]:
    """Build a task result payload with explicit failure account details."""
    items = list(results or [])
    succeeded_items = [item for item in items if bool(getattr(item, "ok", False))]
    failed_items = [item for item in items if not bool(getattr(item, "ok", False))]
    failures: List[Dict[str, Any]] = []
    for item in failed_items[: max(0, int(max_failures))]:
        failures.append(
            {
                "id": int(getattr(item, "account_id", 0) or 0),
                "email": safe_visible(getattr(item, "email", "")),
                "detail": safe_visible(getattr(item, "detail", ""))[:400],
            }
        )
    succeeded = len(succeeded_items)
    failed = len(failed_items)
    message = "%s完成，成功 %s，失败 %s" % (action_label, succeeded, failed)
    if failures:
        preview = "、".join(
            item["email"] or ("#%s" % item["id"]) for item in failures[:3]
        )
        if preview:
            extra = "等 %s 个" % failed if failed > 3 else ""
            message = "%s；失败账号：%s%s" % (message, preview, extra)
    return {
        "count": len(items),
        "succeeded": succeeded,
        "failed": failed,
        "failures": failures,
        "failureTruncated": failed > len(failures),
        "message": message,
    }


def log_account_failures(task: "TaskRecord", failures: List[Dict[str, Any]], *, truncated: bool = False) -> None:
    if not failures:
        return
    task.log("失败账号明细（%s）:" % len(failures))
    for item in failures:
        account_id = item.get("id") or "?"
        email = item.get("email") or ("#%s" % account_id)
        detail = item.get("detail") or "失败"
        task.log("  - %s: %s" % (email, detail))
    if truncated:
        task.log("  … 失败列表已截断，仅展示前 %s 条" % len(failures))


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_visible(value: Any) -> str:
    return str(value or "").replace("—", "-").replace("–", "-")


def normalize_bind_host(host: str) -> str:
    value = (host or "").strip()
    if not value:
        raise ValueError("host 不能为空")
    if value.startswith("[") and value.endswith("]") and len(value) > 2:
        value = value[1:-1]
    return value


def is_loopback_host(host: str) -> bool:
    return normalize_bind_host(host).lower() in LOOPBACK_HOSTS


def is_wildcard_host(host: str) -> bool:
    return normalize_bind_host(host).lower() in WILDCARD_HOSTS


def host_header_hostname(host_header: str) -> Optional[str]:
    host = (host_header or "").strip().lower()
    if not host:
        return None
    if host.startswith("["):
        closing = host.find("]")
        if closing < 0:
            return None
        hostname = host[1:closing]
        suffix = host[closing + 1 :]
        if suffix and not (suffix.startswith(":") and suffix[1:].isdigit()):
            return None
        return hostname
    hostname, separator, port = host.rpartition(":")
    if not separator:
        return host
    if not port.isdigit():
        return None
    return hostname


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
                    outcome = task_result_outcome(result)
                    task.state = outcome
                    if outcome == "partial":
                        task.message = safe_visible(
                            result.get("message") or "任务完成，部分账号失败"
                        )
                    elif outcome == "failed":
                        task.message = safe_visible(
                            result.get("message") or "任务失败"
                        )
                    else:
                        task.message = safe_visible(
                            result.get("message") or "任务完成"
                        )
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
        self._cpa_guard_stop = threading.Event()
        self._cpa_guard_thread: Optional[threading.Thread] = None
        if self.manager.config.auto_import_on_start:
            try:
                self.start_import({})
            except RuntimeError:
                pass

    def start_cpa_guard(self, *, force: bool = False) -> bool:
        """Start background CPA silent-refresh loop. Returns True if started."""
        if self._cpa_guard_thread is not None and self._cpa_guard_thread.is_alive():
            return False
        if not force and not bool(self.manager.config.cpa_guard_enabled):
            return False
        self._cpa_guard_stop.clear()

        def worker() -> None:
            def log(message: str) -> None:
                print("[cpa-guard] %s" % message, flush=True)

            try:
                self.manager.run_cpa_guard_loop(
                    interval_seconds=int(self.manager.config.cpa_guard_interval_seconds),
                    lead_seconds=int(self.manager.config.cpa_guard_lead_seconds),
                    once=False,
                    log=log,
                    cancelled=self._cpa_guard_stop.is_set,
                )
            except Exception as exc:
                # KeyboardInterrupt is process-wide; daemon exit should stay quiet.
                if not isinstance(exc, KeyboardInterrupt):
                    print("[cpa-guard] 守护线程异常退出: %s" % exc, flush=True)

        self._cpa_guard_thread = threading.Thread(
            target=worker,
            name="cpa-guard",
            daemon=True,
        )
        self._cpa_guard_thread.start()
        return True

    def stop_cpa_guard(self, timeout: float = 1.5) -> None:
        """Signal the guard to stop; never block shutdown on a second Ctrl+C."""
        self._cpa_guard_stop.set()
        thread = self._cpa_guard_thread
        self._cpa_guard_thread = None
        if thread is None or not thread.is_alive():
            return
        try:
            thread.join(timeout=max(0.05, float(timeout)))
        except KeyboardInterrupt:
            # User hit Ctrl+C again while we waited for the daemon guard.
            return
        if thread.is_alive():
            print("[cpa-guard] 守护线程仍在收尾，随进程退出", flush=True)

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
        registration_secrets: Dict[str, bool] = {}
        try:
            reference_config = self.manager.reference.load_registration_config()
            for key in SENSITIVE_CONFIG_KEYS:
                registration_secrets[key] = bool(str(reference_config.get(key) or ""))
                if registration_secrets[key]:
                    reference_config[key] = ""
        except Exception as exc:
            reference_error = safe_visible(exc)
        return {
            "manager": asdict(self.manager.config),
            "registration": reference_config,
            "registrationSecrets": registration_secrets,
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

            results = self.manager.batch_login(
                ids,
                log=task.log,
                progress=progress,
                cancelled=lambda: task.cancel_requested,
            )
            summary = summarize_account_results(results, action_label="登录")
            log_account_failures(
                task,
                summary["failures"],
                truncated=bool(summary.get("failureTruncated")),
            )
            return summary

        return self.tasks.start("login", "批量登录", worker, exclusive_group="browser-automation")

    def start_consent(self, payload: Dict[str, Any]) -> TaskRecord:
        ids = self._ids(payload)
        if not ids:
            raise ValueError("没有待授权确认的账号")

        def worker(task: TaskRecord) -> Dict[str, Any]:
            task.log("开始授权确认 %s 个账号（TOS）" % len(ids))

            def progress(result, completed, total):
                task.progress(completed, total, "%s: %s" % (result.email, result.detail))
                task.log("#%s %s: %s" % (result.account_id, result.email, result.detail))

            results = self.manager.consent_accounts(
                ids,
                log=task.log,
                progress=progress,
                cancelled=lambda: task.cancel_requested,
            )
            summary = summarize_account_results(results, action_label="授权确认")
            log_account_failures(
                task,
                summary["failures"],
                truncated=bool(summary.get("failureTruncated")),
            )
            return summary

        return self.tasks.start(
            "consent",
            "授权确认",
            worker,
            exclusive_group="browser-automation",
        )

    def start_cpa_refresh(self, payload: Dict[str, Any]) -> TaskRecord:
        ids = self._ids(payload)
        if not ids:
            raise ValueError("没有待续期的 CPA 账号")

        def worker(task: TaskRecord) -> Dict[str, Any]:
            task.log("开始 CPA 续期 %s 个账号" % len(ids))

            def progress(result, completed, total):
                task.progress(completed, total, "%s: %s" % (result.email, result.detail))
                task.log("#%s %s: %s" % (result.account_id, result.email, result.detail))

            results = self.manager.batch_refresh_cpa(
                ids,
                log=task.log,
                progress=progress,
                cancelled=lambda: task.cancel_requested,
            )
            summary = summarize_account_results(results, action_label="CPA 续期")
            log_account_failures(
                task,
                summary["failures"],
                truncated=bool(summary.get("failureTruncated")),
            )
            return summary

        return self.tasks.start(
            "refresh-cpa",
            "CPA 续期",
            worker,
            exclusive_group="cpa-refresh",
        )

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
                    auto_reset_password=False,
                )
            reset_succeeded = sum(1 for result in reset_results if result.ok)
            login_succeeded = sum(1 for result in login_results if result.ok)
            reset_failures = [
                {
                    "id": int(result.account_id),
                    "email": safe_visible(result.email),
                    "detail": safe_visible("重置失败: %s" % result.detail)[:400],
                }
                for result in reset_results
                if not result.ok
            ]
            login_failures = [
                {
                    "id": int(result.account_id),
                    "email": safe_visible(result.email),
                    "detail": safe_visible("重置后登录失败: %s" % result.detail)[:400],
                }
                for result in login_results
                if not result.ok
            ]
            failures = (reset_failures + login_failures)[:200]
            message = "密码重置 %s 个，自动登录成功 %s 个" % (
                reset_succeeded,
                login_succeeded,
            )
            if failures:
                preview = "、".join(
                    item["email"] or ("#%s" % item["id"]) for item in failures[:3]
                )
                if preview:
                    extra = "等 %s 个" % len(failures) if len(failures) > 3 else ""
                    message = "%s；失败账号：%s%s" % (message, preview, extra)
            log_account_failures(task, failures, truncated=len(reset_failures) + len(login_failures) > len(failures))
            return {
                "resetCount": len(reset_results),
                "resetSucceeded": reset_succeeded,
                "loginCount": len(login_results),
                "loginSucceeded": login_succeeded,
                "failed": len(failures),
                "failures": failures,
                "failureTruncated": (len(reset_failures) + len(login_failures)) > len(failures),
                "message": message,
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
        if task.kind not in (
            "register",
            "login",
            "consent",
            "reset-password",
            "inspect",
            "refresh-cpa",
        ):
            raise RuntimeError("该任务不支持中途取消")
        task.cancel_requested = True
        if task.kind == "register":
            self.manager.registration.cancel()
        elif task.kind == "login":
            self.manager.password_reset.cancel()
            self.manager.login.cancel()
            self.manager.consent.cancel()
        elif task.kind == "consent":
            self.manager.consent.cancel()
            task.message = "正在停止授权确认"
        elif task.kind == "reset-password":
            self.manager.password_reset.cancel()
        elif task.kind == "inspect":
            task.message = "正在停止巡检"
        elif task.kind == "refresh-cpa":
            # May fall back to browser SSO remint via the login worker.
            self.manager.login.cancel()
            task.message = "正在停止 CPA 续期"
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

    def serve(
        self,
        host: str = "127.0.0.1",
        port: int = 8787,
        open_browser: bool = True,
        allow_lan: bool = False,
        cpa_guard: Optional[bool] = None,
    ) -> None:
        bind_host = normalize_bind_host(host)
        if not allow_lan and not is_loopback_host(bind_host):
            raise ValueError(
                "为保护账号凭据，默认只允许绑定本机回环地址；如需局域网访问请加 --lan"
            )
        if allow_lan and is_loopback_host(bind_host):
            bind_host = "0.0.0.0"
        application = self
        enable_guard = (
            bool(self.manager.config.cpa_guard_enabled)
            if cpa_guard is None
            else bool(cpa_guard)
        )

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
                hostname = host_header_hostname(self.headers.get("Host", ""))
                if not hostname:
                    return False
                if hostname in LOOPBACK_HOSTS:
                    return True
                if not allow_lan:
                    return False
                if is_wildcard_host(bind_host):
                    return True
                return hostname == bind_host.lower()

            def _allow_request(self) -> bool:
                if self._trusted_host():
                    return True
                if allow_lan:
                    self._error(403, "Host 头不被当前绑定地址允许")
                else:
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
                    if parsed.path == "/assets/app.bundle.js":
                        self._bytes((ASSET_DIR / "app.bundle.js").read_bytes(), "text/javascript; charset=utf-8")
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
                    elif parsed.path == "/api/consent":
                        self._json({"task": application.start_consent(payload).serialize(False)}, 202)
                    elif parsed.path == "/api/refresh-cpa":
                        self._json(
                            {"task": application.start_cpa_refresh(payload).serialize(False)},
                            202,
                        )
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
        if ":" in bind_host:
            class IPv6ThreadingHTTPServer(ThreadingHTTPServer):
                address_family = socket.AF_INET6

            server_class = IPv6ThreadingHTTPServer
        server = server_class((bind_host, int(port)), Handler)
        server.daemon_threads = True
        self._server = server
        bound_port = server.server_address[1]
        if allow_lan:
            local_url = "http://127.0.0.1:%s" % bound_port
            print("Grok Account Manager: %s (监听 %s)" % (local_url, bind_host), flush=True)
            print(
                "已开启局域网访问：请用本机局域网 IP 访问，例如 http://<局域网IP>:%s" % bound_port,
                flush=True,
            )
            print("警告：局域网内其他设备可访问此管理端；请确保网络可信。", flush=True)
            browser_url = local_url
        else:
            display_host = "[%s]" % bind_host if ":" in bind_host else bind_host
            browser_url = "http://%s:%s" % (display_host, bound_port)
            print("Grok Account Manager: %s" % browser_url, flush=True)
        if open_browser:
            threading.Timer(0.4, lambda: webbrowser.open(browser_url)).start()
        if enable_guard:
            if self.start_cpa_guard(force=True):
                print(
                    "CPA 守护已随管理端启动（interval=%ss lead=%ss）"
                    % (
                        self.manager.config.cpa_guard_interval_seconds,
                        self.manager.config.cpa_guard_lead_seconds,
                    ),
                    flush=True,
                )
            else:
                print("CPA 守护未能启动（可能已在运行）", flush=True)
        else:
            print("CPA 守护未启用（配置关闭或传入 --no-cpa-guard）", flush=True)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            print("\n正在停止管理端…", flush=True)
        finally:
            try:
                self.stop_cpa_guard(timeout=1.0)
            except KeyboardInterrupt:
                pass
            try:
                server.server_close()
            except Exception:
                pass
