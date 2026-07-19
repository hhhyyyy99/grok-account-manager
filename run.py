#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path
from typing import Iterable, List, Optional


ROOT = Path(__file__).resolve().parent
COMMANDS = frozenset(
    (
        "ui",
        "list",
        "import",
        "inspect",
        "login",
        "reset-password",
        "register",
        "config",
        "delete",
        "cpa-guard",
    )
)
REQUIRED_MODULES = ("DrissionPage", "curl_cffi", "requests")


def find_venv_python(root: Path = ROOT) -> Optional[Path]:
    candidates = (
        root / ".venv" / "bin" / "python",
        root / ".venv" / "Scripts" / "python.exe",
    )
    for candidate in candidates:
        if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
            return candidate.absolute()
    return None


def is_current_python(executable: Path) -> bool:
    return executable.absolute() == Path(sys.executable).absolute()


def normalize_cli_args(argv: Iterable[str]) -> List[str]:
    args = list(argv)
    if not args:
        return ["ui"]
    if args[0] in COMMANDS:
        return args
    if args[0].startswith("-"):
        return ["ui", *args]
    return args


def find_uv() -> Optional[str]:
    return shutil.which("uv")


def activate_project_environment(argv: Iterable[str]) -> None:
    args = list(argv)
    executable = find_venv_python()
    if executable is not None and is_current_python(executable):
        return
    script = str(ROOT / "run.py")
    if executable is not None:
        try:
            os.execv(str(executable), [str(executable), script, *args])
        except OSError as exc:
            raise RuntimeError("无法使用项目虚拟环境启动: %s" % exc) from exc
        return
    uv = find_uv()
    if uv:
        try:
            os.execv(
                uv,
                [
                    uv,
                    "run",
                    "--locked",
                    "--project",
                    str(ROOT),
                    "python",
                    script,
                    *args,
                ],
            )
        except OSError as exc:
            raise RuntimeError("无法通过 uv 启动项目环境: %s" % exc) from exc


def runtime_error() -> str:
    version = sys.version_info
    if version[:2] != (3, 13):
        return (
            "当前 Python 为 %s.%s.%s，本项目需要 Python 3.13.x。\n"
            "请安装 uv 后在项目根目录执行:\n"
            "  uv python install 3.13\n"
            "  uv sync --locked\n"
            "  uv run --locked python run.py"
            % version[:3]
        )
    missing = [
        module
        for module in REQUIRED_MODULES
        if importlib.util.find_spec(module) is None
    ]
    if missing:
        return (
            "当前 Python 环境缺少依赖: %s\n"
            "请在项目根目录执行:\n"
            "  uv sync --locked\n"
            "  uv run --locked python run.py"
            % ", ".join(missing)
        )
    return ""


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    activate_project_environment(args)
    error = runtime_error()
    if error:
        print(error, file=sys.stderr)
        return 2

    os.chdir(ROOT)
    from grok_manager.cli import main as cli_main

    return int(cli_main(normalize_cli_args(args)))


if __name__ == "__main__":
    raise SystemExit(main())
