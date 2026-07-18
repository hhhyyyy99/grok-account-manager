from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, List, Optional

from .models import Account, STATUS_LABELS
from .reference import RegistrationRequest
from .service import GrokManager


def _parse_ids(raw: str) -> List[int]:
    values = []
    for part in str(raw or "").replace("，", ",").split(","):
        text = part.strip()
        if not text:
            continue
        try:
            value = int(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("账号 ID 必须是整数: %s" % text) from exc
        if value > 0:
            values.append(value)
    return values


def _selected_ids(manager: GrokManager, ids: str, all_accounts: bool) -> List[int]:
    parsed = _parse_ids(ids)
    if parsed:
        return parsed
    if all_accounts:
        return [account.id for account in manager.store.list_accounts()]
    return []


def _account_summary(account: Account) -> dict:
    return {
        "id": account.id,
        "email": account.email,
        "status": account.status,
        "status_label": account.status_label,
        "sso_status": account.sso_status,
        "sso_status_label": account.sso_status_label,
        "sso_expires_at": account.sso_expires_at,
        "cpa_status": account.cpa_status,
        "cpa_status_label": account.cpa_status_label,
        "cpa_expires_at": account.token_expires_at,
        "last_checked_at": account.last_checked_at,
        "last_login_at": account.last_login_at,
        "has_password": bool(account.password),
        "has_sso": bool(account.sso_token),
        "has_access_token": bool(account.access_token),
        "source": account.source,
    }


def command_list(args: argparse.Namespace) -> int:
    manager = GrokManager()
    accounts = manager.store.list_accounts(search=args.search, status=args.status, limit=args.limit)
    if args.json:
        print(json.dumps([_account_summary(account) for account in accounts], ensure_ascii=False, indent=2))
        return 0
    if not accounts:
        print("暂无账号。可先批量注册，或运行 `python3 -m grok_manager import` 导入历史产物。")
        return 0
    print("ID    总状态      SSO         CPA         邮箱")
    print("----  ----------  ----------  ----------  ----------------------------------------")
    for account in accounts:
        print(
            "%-4s  %-10s  %-10s  %-10s  %s"
            % (
                account.id,
                account.status_label,
                account.sso_status_label,
                account.cpa_status_label,
                account.email,
            )
        )
    return 0


def command_import(args: argparse.Namespace) -> int:
    manager = GrokManager()
    files = [Path(value).expanduser().resolve() for value in args.file] if args.file else None
    accounts = manager.import_reference_accounts(files)
    print("已导入/更新 %s 个账号" % len(accounts))
    return 0


def command_inspect(args: argparse.Namespace) -> int:
    manager = GrokManager()
    ids = _selected_ids(manager, args.ids, args.all)
    if not ids:
        print("没有待巡检账号，请指定 --ids 或 --all", file=sys.stderr)
        return 2

    def progress(result, completed, total):
        print("[%s/%s] #%s %s - %s" % (completed, total, result.account_id, STATUS_LABELS.get(result.status, result.status), result.detail))

    results = manager.inspect_accounts(ids, live=not args.local, progress=progress)
    failures = sum(1 for result in results if result.status in ("unknown", "expired", "invalid", "error", "needs_login"))
    print("巡检完成: %s 个，需处理 %s 个" % (len(results), failures))
    return 1 if failures else 0


def command_login(args: argparse.Namespace) -> int:
    manager = GrokManager()
    if args.expired:
        ids = manager.relogin_candidate_ids()
    else:
        ids = _selected_ids(manager, args.ids, args.all)
    if not ids:
        print("没有待登录账号，请指定 --ids/--all，或先巡检后使用 --expired", file=sys.stderr)
        return 2

    def progress(result, completed, total):
        label = "成功" if result.ok else "失败"
        print("[%s/%s] #%s %s %s - %s" % (completed, total, result.account_id, result.email, label, result.detail))

    results = manager.batch_login(ids, log=lambda line: print("[login] %s" % line), progress=progress)
    success = sum(1 for result in results if result.ok)
    print("批量登录完成: 成功 %s，失败 %s" % (success, len(results) - success))
    return 0 if success == len(results) else 1


def command_reset_password(args: argparse.Namespace) -> int:
    manager = GrokManager()
    ids = _selected_ids(manager, args.ids, args.all)
    if not ids:
        print("没有待重置密码账号，请指定 --ids 或 --all", file=sys.stderr)
        return 2

    def progress(result, completed, total):
        label = "成功" if result.ok else "失败"
        print("[%s/%s] #%s %s %s - %s" % (
            completed, total, result.account_id, result.email, label, result.detail
        ))

    reset_results = manager.reset_passwords(
        ids, log=lambda line: print("[reset] %s" % line), progress=progress
    )
    reset_ids = [result.account_id for result in reset_results if result.ok]
    if not reset_ids:
        print("密码重置完成：没有成功账号")
        return 1
    login_results = manager.batch_login(
        reset_ids,
        log=lambda line: print("[login] %s" % line),
        progress=progress,
        auto_reset_password=False,
    )
    success = sum(1 for result in login_results if result.ok)
    print("密码重置并登录完成: 重置 %s 个，登录成功 %s 个" % (len(reset_ids), success))
    return 0 if success == len(reset_ids) else 1

def command_register(args: argparse.Namespace) -> int:
    manager = GrokManager()
    request = RegistrationRequest(args.count, args.threads, args.mint_workers)
    result = manager.run_registration(request, log=lambda line: print(line, flush=True))
    print(
        "注册任务完成: return_code=%s, 导入=%s, accounts=%s"
        % (result.return_code, result.imported_count, result.accounts_file)
    )
    if result.error:
        print(result.error, file=sys.stderr)
    return 0 if result.ok else 1


def command_config_check(args: argparse.Namespace) -> int:
    manager = GrokManager()
    checks = manager.diagnostics()
    for ok, message in checks:
        print("%s %s" % ("✓" if ok else "✗", message))
    return 0 if checks and all(ok for ok, _ in checks) else 1


def command_config_show(args: argparse.Namespace) -> int:
    manager = GrokManager()
    print(json.dumps(asdict(manager.config), ensure_ascii=False, indent=2))
    return 0


def command_delete(args: argparse.Namespace) -> int:
    manager = GrokManager()
    ids = _parse_ids(args.ids)
    if not ids:
        print("请通过 --ids 指定账号", file=sys.stderr)
        return 2
    deleted = manager.store.delete(ids)
    print("已从管理库删除 %s 个账号；注册产物文件未改动" % deleted)
    return 0


def command_ui(args: argparse.Namespace) -> int:
    from .web import GrokWebApplication

    application = GrokWebApplication()
    try:
        application.serve(
            host=getattr(args, "host", "127.0.0.1"),
            port=getattr(args, "port", 8787),
            open_browser=not getattr(args, "no_browser", False),
        )
        return 0
    except (OSError, ValueError) as exc:
        print("管理端启动失败: %s" % exc, file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Grok 账号管理器")
    subparsers = parser.add_subparsers(dest="command")

    ui = subparsers.add_parser("ui", help="启动本地 Web 管理端")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8787)
    ui.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ui.set_defaults(handler=command_ui)

    listing = subparsers.add_parser("list", help="列出管理库账号")
    listing.add_argument("--search", default="")
    listing.add_argument("--status", default="")
    listing.add_argument("--limit", type=int, default=None)
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(handler=command_list)

    importer = subparsers.add_parser("import", help="导入本应用的账号产物")
    importer.add_argument("--file", action="append", default=[], help="指定 accounts.txt，可重复")
    importer.set_defaults(handler=command_import)

    inspect = subparsers.add_parser("inspect", help="批量巡检 token")
    inspect.add_argument("--ids", default="", help="逗号分隔账号 ID")
    inspect.add_argument("--all", action="store_true", help="巡检全部账号")
    inspect.add_argument("--local", action="store_true", help="只检查本地到期时间，不发网络请求")
    inspect.set_defaults(handler=command_inspect)

    login = subparsers.add_parser("login", help="批量登录并重新获取 token")
    login.add_argument("--ids", default="", help="逗号分隔账号 ID")
    login.add_argument("--all", action="store_true")
    login.add_argument("--expired", action="store_true", help="登录巡检判定为过期/无效/待登录的账号")
    login.set_defaults(handler=command_login)

    reset_password = subparsers.add_parser("reset-password", help="重置邮箱密码并重新登录")
    reset_password.add_argument("--ids", default="", help="逗号分隔账号 ID")
    reset_password.add_argument("--all", action="store_true")
    reset_password.set_defaults(handler=command_reset_password)

    register = subparsers.add_parser("register", help="调用内置运行时批量注册")
    register.add_argument("--count", type=int, default=1)
    register.add_argument("--threads", type=int, default=1)
    register.add_argument("--mint-workers", type=int, default=1)
    register.set_defaults(handler=command_register)

    config = subparsers.add_parser("config", help="查看/检查配置")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    check = config_sub.add_parser("check", help="检查内置注册运行环境")
    check.set_defaults(handler=command_config_check)
    show = config_sub.add_parser("show", help="显示管理端配置")
    show.set_defaults(handler=command_config_show)

    delete = subparsers.add_parser("delete", help="仅从管理库删除账号")
    delete.add_argument("--ids", required=True)
    delete.set_defaults(handler=command_delete)
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.command:
        return command_ui(args)
    return int(args.handler(args))
