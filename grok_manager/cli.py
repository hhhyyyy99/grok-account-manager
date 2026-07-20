from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, List, Optional

from .models import Account, STATUS_LABELS
from .paths import PROJECT_ROOT, VAULT_FILE
from .reference import RegistrationRequest
from .service import GrokManager
from .vault import CredentialVault, VaultError


_CURRENT_VAULT: Optional[CredentialVault] = None
VAULT_PASSWORD_ENV = "GROK_MANAGER_VAULT_PASSWORD"


def load_project_dotenv(path: Optional[Path] = None) -> Path:
    """Load KEY=VALUE pairs from a local .env without overriding existing env."""
    env_path = Path(path) if path is not None else PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return env_path
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return env_path
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[key] = value
    return env_path


def vault_password_from_env() -> str:
    return str(os.environ.get(VAULT_PASSWORD_ENV) or "").strip()


def unlock_vault() -> CredentialVault:
    load_project_dotenv()
    vault = CredentialVault(VAULT_FILE)
    password = vault_password_from_env()
    if not vault.is_initialized:
        if not password:
            print(
                "首次启动需要创建凭据保险库主密码（至少 12 个字符）。\n"
                "也可写入环境变量 %s 或项目根目录 .env 后重启。"
                % VAULT_PASSWORD_ENV,
                file=sys.stderr,
            )
            password = getpass.getpass("创建主密码: ")
            confirmation = getpass.getpass("再次输入主密码: ")
            if password != confirmation:
                raise VaultError("两次输入的主密码不一致")
        vault.initialize(password)
        return vault
    if not password:
        password = getpass.getpass("请输入凭据保险库主密码: ")
    vault.unlock(password)
    return vault


def _manager() -> GrokManager:
    if _CURRENT_VAULT is None:
        raise VaultError("应用尚未解锁凭据保险库")
    return GrokManager(vault=_CURRENT_VAULT)

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
    manager = _manager()
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
    manager = _manager()
    files = [Path(value).expanduser().resolve() for value in args.file] if args.file else None
    accounts = manager.import_reference_accounts(files)
    print("已导入/更新 %s 个账号" % len(accounts))
    return 0


def command_inspect(args: argparse.Namespace) -> int:
    manager = _manager()
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
    manager = _manager()
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
    manager = _manager()
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
    manager = _manager()
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
    manager = _manager()
    checks = manager.diagnostics()
    for ok, message in checks:
        print("%s %s" % ("✓" if ok else "✗", message))
    return 0 if checks and all(ok for ok, _ in checks) else 1


def command_config_show(args: argparse.Namespace) -> int:
    manager = _manager()
    print(json.dumps(asdict(manager.config), ensure_ascii=False, indent=2))
    return 0


def command_delete(args: argparse.Namespace) -> int:
    manager = _manager()
    ids = _parse_ids(args.ids)
    if not ids:
        print("请通过 --ids 指定账号", file=sys.stderr)
        return 2
    deleted = manager.store.delete(ids)
    print("已从管理库删除 %s 个账号；注册产物文件未改动" % deleted)
    return 0


def command_cpa_guard(args: argparse.Namespace) -> int:
    manager = _manager()
    interval = (
        int(args.interval)
        if args.interval is not None
        else int(manager.config.cpa_guard_interval_seconds)
    )
    lead = (
        int(args.lead)
        if args.lead is not None
        else int(manager.config.cpa_guard_lead_seconds)
    )
    once = bool(args.once)
    stop = {"value": False}

    def cancelled() -> bool:
        return bool(stop["value"])

    def log(message: str) -> None:
        print("[cpa-guard] %s" % message, flush=True)

    try:
        manager.run_cpa_guard_loop(
            interval_seconds=interval,
            lead_seconds=lead,
            once=once,
            log=log,
            cancelled=cancelled,
        )
    except KeyboardInterrupt:
        stop["value"] = True
        log("收到中断，正在退出")
        return 130
    return 0


def command_cpa_sync(args: argparse.Namespace) -> int:
    manager = _manager()
    ids = _selected_ids(manager, getattr(args, "ids", ""), bool(getattr(args, "all", False)))
    if getattr(args, "ids", "") and not ids:
        print("没有匹配的账号 ID", file=sys.stderr)
        return 2
    selected = ids or None

    def log(message: str) -> None:
        print("[cpa-sync] %s" % message, flush=True)

    results = manager.sync_cpa_hotload_accounts(
        selected,
        log=log,
        push_when_manager_newer=not bool(getattr(args, "pull_only", False)),
        reinspect=not bool(getattr(args, "no_inspect", False)),
    )
    pulled = sum(1 for item in results if item.action == "pull")
    pushed = sum(1 for item in results if item.action == "push")
    failed = sum(1 for item in results if not item.ok)
    print(
        "CPA hotload 同步完成：处理 %s，回灌 %s，推送 %s，失败 %s"
        % (len(results), pulled, pushed, failed)
    )
    return 1 if failed else 0


def command_ui(args: argparse.Namespace) -> int:
    from .web import GrokWebApplication

    application = GrokWebApplication(_manager())
    cpa_guard: Optional[bool]
    if bool(getattr(args, "no_cpa_guard", False)):
        cpa_guard = False
    elif bool(getattr(args, "cpa_guard", False)):
        cpa_guard = True
    else:
        cpa_guard = None
    try:
        application.serve(
            host=getattr(args, "host", "127.0.0.1"),
            port=getattr(args, "port", 8787),
            open_browser=not getattr(args, "no_browser", False),
            allow_lan=bool(getattr(args, "lan", False)),
            cpa_guard=cpa_guard,
        )
        return 0
    except (OSError, ValueError) as exc:
        print("管理端启动失败: %s" % exc, file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Grok 账号管理器")
    subparsers = parser.add_subparsers(dest="command")

    ui = subparsers.add_parser("ui", help="启动本地 Web 管理端")
    ui.add_argument(
        "--host",
        default="127.0.0.1",
        help="绑定地址；默认 127.0.0.1。配合 --lan 可指定网卡地址",
    )
    ui.add_argument("--port", type=int, default=8787)
    ui.add_argument(
        "--lan",
        action="store_true",
        help="允许局域网访问（默认绑定 0.0.0.0，并放宽 Host 校验）",
    )
    ui.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ui.add_argument(
        "--cpa-guard",
        action="store_true",
        help="强制随管理端启动 CPA 守护（覆盖配置关闭）",
    )
    ui.add_argument(
        "--no-cpa-guard",
        action="store_true",
        help="不随管理端启动 CPA 守护（覆盖配置开启）",
    )
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

    guard = subparsers.add_parser(
        "cpa-guard",
        help="CPA access_token 守护进程：临近过期时 silent refresh，refresh 失效则标记 CPA 过期",
    )
    guard.add_argument(
        "--interval",
        type=int,
        default=None,
        help="轮询间隔秒数（默认读取配置 cpa_guard_interval_seconds，通常 300）",
    )
    guard.add_argument(
        "--lead",
        type=int,
        default=None,
        help="提前续期秒数（默认读取配置 cpa_guard_lead_seconds，通常 1800）",
    )
    guard.add_argument(
        "--once",
        action="store_true",
        help="只跑一轮后退出（方便 cron / 测试）",
    )
    guard.set_defaults(handler=command_cpa_guard)

    sync = subparsers.add_parser(
        "cpa-sync",
        help="管理库与 CPA hotload 双向同步（以新为准）",
    )
    sync.add_argument("--ids", default="", help="逗号分隔账号 ID；默认全部相关账号")
    sync.add_argument("--all", action="store_true", help="同步全部账号")
    sync.add_argument(
        "--pull-only",
        action="store_true",
        help="只从 hotload 回灌，不把管理库更新的凭据推回去",
    )
    sync.add_argument(
        "--no-inspect",
        action="store_true",
        help="同步后不自动复核",
    )
    sync.set_defaults(handler=command_cpa_sync)
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    global _CURRENT_VAULT
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        _CURRENT_VAULT = unlock_vault()
    except (VaultError, EOFError, KeyboardInterrupt) as exc:
        print("凭据保险库解锁失败: %s" % exc, file=sys.stderr)
        return 2
    try:
        if not args.command:
            return command_ui(args)
        return int(args.handler(args))
    finally:
        if _CURRENT_VAULT is not None:
            _CURRENT_VAULT.lock()
        _CURRENT_VAULT = None
