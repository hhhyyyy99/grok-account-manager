from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional

from .models import Account, ConsentResult
from .store import AccountStore


LogCallback = Callable[[str], None]
ProgressCallback = Callable[[ConsentResult, int, int], Optional[ConsentResult]]
CancelCallback = Callable[[], bool]


@dataclass(frozen=True)
class ConsentSettings:
    workers: int = 1
    timeout_seconds: int = 120
    proxy: str = ""
    headless: bool = False


class BatchConsentService:
    """Browser-side consent: TOS gate only."""

    def __init__(self, store: AccountStore):
        self.store = store
        self._cancel = threading.Event()
        self._running = False
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._running

    def cancel(self) -> bool:
        self._cancel.set()
        return True

    def consent_accounts(
        self,
        account_ids: Iterable[int],
        settings: ConsentSettings,
        log: Optional[LogCallback] = None,
        progress: Optional[ProgressCallback] = None,
        cancelled: Optional[CancelCallback] = None,
    ) -> List[ConsentResult]:
        log = log or (lambda _message: None)
        with self._lock:
            if self._running:
                raise RuntimeError("已有授权确认任务正在运行")
            self._running = True
            self._cancel.clear()

        try:
            requested_ids: List[int] = []
            seen: set[int] = set()
            for value in account_ids:
                account_id = int(value or 0)
                if account_id > 0 and account_id not in seen:
                    seen.add(account_id)
                    requested_ids.append(account_id)
            if not requested_ids:
                return []

            accounts = {
                account.id: account for account in self.store.get_many(requested_ids)
            }
            results_by_id: Dict[int, ConsentResult] = {}
            pending: List[Account] = []
            for account_id in requested_ids:
                account = accounts.get(account_id)
                if account is None:
                    results_by_id[account_id] = ConsentResult(
                        account_id, "", False, "账号不存在"
                    )
                    continue
                if not str(account.sso_token or "").strip():
                    results_by_id[account_id] = ConsentResult(
                        account_id,
                        account.email,
                        False,
                        "缺少 SSO cookie，请先登录",
                    )
                    continue
                pending.append(account)

            total = len(requested_ids)
            completed = 0
            for account_id, result in list(results_by_id.items()):
                completed += 1
                if progress:
                    progress(result, completed, total)

            if not pending:
                return [results_by_id[account_id] for account_id in requested_ids]

            workers = max(1, min(int(settings.workers or 1), 4, len(pending)))
            timeout = max(30, int(settings.timeout_seconds or 120))

            def should_stop() -> bool:
                if self._cancel.is_set():
                    return True
                if cancelled and cancelled():
                    return True
                return False

            def run_one(account: Account) -> ConsentResult:
                if should_stop():
                    return ConsentResult(
                        account.id, account.email, False, "任务已取消"
                    )
                try:
                    return _consent_one_account(
                        account,
                        settings=settings,
                        timeout_seconds=timeout,
                        log=lambda message: log("[%s] %s" % (account.email, message)),
                        should_stop=should_stop,
                    )
                except Exception as exc:
                    return ConsentResult(
                        account.id,
                        account.email,
                        False,
                        "授权确认异常: %s" % exc,
                    )

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(run_one, account): account for account in pending}
                for future in as_completed(futures):
                    account = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = ConsentResult(
                            account.id,
                            account.email,
                            False,
                            "授权确认异常: %s" % exc,
                        )
                    results_by_id[account.id] = result
                    completed += 1
                    log(
                        "[%s] %s"
                        % (result.email or ("#%s" % result.account_id), result.detail)
                    )
                    if progress:
                        progress(result, completed, total)

            ordered: List[ConsentResult] = []
            for account_id in requested_ids:
                result = results_by_id.get(account_id)
                if result is None:
                    result = ConsentResult(account_id, "", False, "授权确认未返回结果")
                ordered.append(result)
            return ordered
        finally:
            with self._lock:
                self._running = False


def _sleep(seconds: float, should_stop: CancelCallback) -> bool:
    end = time.time() + max(0.0, float(seconds))
    while time.time() < end:
        if should_stop():
            return False
        time.sleep(min(0.25, end - time.time()))
    return not should_stop()


def _inject_sso(page: Any, sso: str) -> None:
    token = str(sso or "").strip()
    if not token:
        return
    cookies = []
    for domain in (".x.ai", "accounts.x.ai", ".grok.com", "grok.com"):
        cookies.append({"name": "sso", "value": token, "domain": domain, "path": "/"})
        cookies.append(
            {"name": "sso-rw", "value": token, "domain": domain, "path": "/"}
        )
    try:
        page.set.cookies(cookies)
    except Exception:
        for cookie in cookies:
            try:
                page.set.cookies([cookie])
            except Exception:
                pass


def _consent_one_account(
    account: Account,
    *,
    settings: ConsentSettings,
    timeout_seconds: int,
    log: LogCallback,
    should_stop: CancelCallback,
) -> ConsentResult:
    from grok_register.cpa_xai import browser_confirm

    sso = str(account.sso_token or "").strip()
    browser = page = None
    started = time.time()

    try:
        browser, page = browser_confirm.create_standalone_page(
            proxy=settings.proxy or None,
            headless=bool(settings.headless),
            log=log,
        )
        if should_stop():
            return ConsentResult(account.id, account.email, False, "任务已取消")

        log("open accounts.x.ai to attach SSO")
        page.get("https://accounts.x.ai/account")
        if not _sleep(1.0, should_stop):
            return ConsentResult(account.id, account.email, False, "任务已取消")
        _inject_sso(page, sso)

        stop_event = threading.Event()

        def watch() -> None:
            while not stop_event.is_set():
                if should_stop():
                    stop_event.set()
                    return
                time.sleep(0.2)

        threading.Thread(target=watch, name="consent-cancel-watch", daemon=True).start()
        remaining = max(20, int(timeout_seconds - (time.time() - started)))
        result = browser_confirm.prepare_account_gates(
            page,
            log=log,
            timeout_sec=float(remaining),
            stop_event=stop_event,
        )
        stop_event.set()
        if should_stop():
            return ConsentResult(account.id, account.email, False, "任务已取消")

        tos_ok = bool(result.get("tos_ok") or result.get("ok"))
        detail = str(
            result.get("detail")
            or ("授权确认完成" if tos_ok else "授权确认未通过")
        )
        if time.time() - started > timeout_seconds and not tos_ok:
            detail = "超时后结果: " + detail
        return ConsentResult(
            account.id,
            account.email,
            bool(result.get("ok")),
            detail,
            tos_ok=tos_ok,
        )
    finally:
        if browser is not None:
            try:
                browser_confirm.close_standalone(browser)
            except Exception:
                pass
