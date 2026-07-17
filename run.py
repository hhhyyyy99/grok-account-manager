#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Iterable, List, Optional


ROOT = Path(__file__).resolve().parent
COMMANDS = frozenset(
    ("ui", "list", "import", "inspect", "login", "register", "config", "delete")
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


def activate_project_venv(argv: Iterable[str]) -> None:
    executable = find_venv_python()
    if executable is None or is_current_python(executable):
        return
    script = str(ROOT / "run.py")
    try:
        os.execv(str(executable), [str(executable), script, *list(argv)])
    except OSError as exc:
        raise RuntimeError("无法使用项目虚拟环境启动: %s" % exc) from exc


def runtime_error() -> str:
    version = sys.version_info
    if version[:2] != (3, 13):
        return (
            "当前 Python 为 %s.%s.%s，本项目需要 Python 3.13.x。\n"
            "请先执行:\n"
            "  python3.13 -m venv .venv\n"
            "  .venv/bin/python -m pip install -e ."
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
            "请执行: %s -m pip install -e ."
            % (", ".join(missing), sys.executable)
        )
    return ""


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    activate_project_venv(args)
    error = runtime_error()
    if error:
        print(error, file=sys.stderr)
        return 2

    os.chdir(ROOT)
    from grok_manager.cli import main as cli_main

    return int(cli_main(normalize_cli_args(args)))


if __name__ == "__main__":
    raise SystemExit(main())
