from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, TypeVar

from .paths import JOBS_DIR, ensure_data_dirs
from .reference import ReferenceProject


LogCallback = Callable[[str], None]
T = TypeVar("T")
ResultParser = Callable[[str], T]
ProgressHook = Callable[[T, int, int], Optional[T]]


def handle_worker_log(payload: str, log: LogCallback) -> None:
    try:
        value = json.loads(payload)
        email = str(value.get("email") or "")
        message = str(value.get("message") or value.get("error") or value)
        log("[%s] %s" % (email, message) if email else message)
    except (json.JSONDecodeError, AttributeError):
        log(payload)


class BatchWorkerProcess:
    """Shared subprocess runner for login/password-reset workers."""

    def __init__(
        self,
        project: ReferenceProject,
        python_executable: str,
        *,
        job_glob: str,
        busy_error: str,
    ):
        self.project = project
        self.python_executable = python_executable
        self.job_glob = job_glob
        self.busy_error = busy_error
        self._lock = threading.Lock()
        self._process: Optional[subprocess.Popen] = None
        self.cleanup_stale_inputs()

    def cleanup_stale_inputs(self) -> None:
        ensure_data_dirs()
        stale_before = time.time() - 24 * 60 * 60
        for path in JOBS_DIR.glob(self.job_glob):
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

    def run(
        self,
        worker_command: str,
        document: Dict[str, Any],
        *,
        log: LogCallback,
        parse_result: ResultParser[T],
        progress: Optional[ProgressHook[T]] = None,
        completed: int = 0,
        total: int = 0,
        stdin_missing_message: str,
        start_failed_message: str,
        exit_failed_message: str,
    ) -> Tuple[List[T], Set[int], int]:
        results: List[T] = []
        parsed_ids: Set[int] = set()
        process: Optional[subprocess.Popen] = None
        worker_script = Path(__file__).with_name("reference_worker.py")
        command = [
            self.python_executable,
            str(worker_script),
            worker_command,
            "--input",
            "-",
        ]
        env = self.project.environment()
        env["PYTHONUNBUFFERED"] = "1"
        try:
            with self._lock:
                if self._process is not None and self._process.poll() is None:
                    raise RuntimeError(self.busy_error)
                self._process = subprocess.Popen(
                    command,
                    cwd=str(self.project.work_dir),
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=(os.name != "nt"),
                )
                process = self._process
            if hasattr(process, "stdin"):
                if process.stdin is None:
                    raise OSError(stdin_missing_message)
                process.stdin.write(json.dumps(document, ensure_ascii=False))
                process.stdin.close()
                process.stdin = None
            if process.stdout is not None:
                for line in process.stdout:
                    text = line.rstrip("\r\n")
                    if text.startswith("GM_LOG "):
                        handle_worker_log(text[7:], log)
                    elif text.startswith("GM_RESULT "):
                        result = parse_result(text[10:])
                        results.append(result)
                        account_id = int(getattr(result, "account_id", 0) or 0)
                        if account_id:
                            parsed_ids.add(account_id)
                        completed += 1
                        if progress:
                            replacement = progress(result, completed, total)
                            if (
                                replacement is not None
                                and replacement is not result
                            ):
                                results[-1] = replacement
                                replacement_id = int(
                                    getattr(replacement, "account_id", 0) or 0
                                )
                                if replacement_id:
                                    parsed_ids.add(replacement_id)
                    elif text.startswith("GM_FATAL "):
                        handle_worker_log(text[9:], log)
                    elif text:
                        log(text)
            return_code = process.wait()
            if return_code not in (0, 2):
                log(exit_failed_message % return_code)
        except OSError as exc:
            log(start_failed_message % exc)
        finally:
            self._reap_process(process)
        return results, parsed_ids, completed

    def _reap_process(self, process: Optional[subprocess.Popen]) -> None:
        if process is None:
            with self._lock:
                if self._process is process:
                    self._process = None
            return
        try:
            stdin = getattr(process, "stdin", None)
            if stdin is not None:
                try:
                    stdin.close()
                except OSError:
                    pass
                try:
                    process.stdin = None
                except (AttributeError, TypeError):
                    pass
            still_running = True
            try:
                still_running = process.poll() is None
            except Exception:
                still_running = False
            if still_running:
                if os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except (OSError, ProcessLookupError):
                        try:
                            process.terminate()
                        except OSError:
                            pass
                else:
                    try:
                        process.terminate()
                    except OSError:
                        pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if os.name != "nt":
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except (OSError, ProcessLookupError):
                            try:
                                process.kill()
                            except OSError:
                                pass
                    else:
                        try:
                            process.kill()
                        except OSError:
                            pass
                    try:
                        process.wait(timeout=2)
                    except (subprocess.TimeoutExpired, OSError):
                        pass
                except TypeError:
                    # Tests may stub wait() without a timeout argument.
                    try:
                        process.wait()
                    except OSError:
                        pass
                except OSError:
                    pass
            stdout = getattr(process, "stdout", None)
            if stdout is not None:
                try:
                    stdout.close()
                except OSError:
                    pass
        finally:
            with self._lock:
                if self._process is process:
                    self._process = None
