"""Background process management (daemon spawn/kill), per-OS.

Windows: DETACHED_PROCESS (+pythonw when available).
POSIX:   start_new_session=True (setsid) — the double-fork of the Genevra
         daemon is unnecessary when the parent exits immediately.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from . import current_os


def python_executable(windowed: bool = False) -> str:
    """Interpreter of the current venv; pythonw.exe for windowed daemons on Windows."""
    exe = sys.executable
    if windowed and current_os() == "windows":
        pythonw = Path(exe).with_name("pythonw.exe")
        if pythonw.exists():
            return str(pythonw)
    return exe


def rotate_log(log_file: Path, max_bytes: int = 5_000_000, keep: int = 3) -> None:
    """Size-based log rotation with bounded retention (AUD-18): when log_file
    exceeds max_bytes, shift .N->.N+1 and start fresh; keep at most `keep`
    old files. Best-effort — never raises."""
    try:
        if not log_file.exists() or log_file.stat().st_size < max_bytes:
            return
        oldest = log_file.with_name(f"{log_file.name}.{keep}")
        if oldest.exists():
            oldest.unlink()
        for n in range(keep - 1, 0, -1):
            src = log_file.with_name(f"{log_file.name}.{n}")
            if src.exists():
                src.replace(log_file.with_name(f"{log_file.name}.{n + 1}"))
        log_file.replace(log_file.with_name(f"{log_file.name}.1"))
    except OSError:
        pass


def spawn_detached(args: list[str], log_file: Path) -> int:
    """Start a long-lived background process, return its PID."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    rotate_log(log_file)                       # bound daemon log growth (AUD-18)
    log = open(log_file, "ab")
    if current_os() == "windows":
        DETACHED_PROCESS = 0x00000008
        CREATE_NO_WINDOW = 0x08000000
        proc = subprocess.Popen(
            args,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
            close_fds=True,
        )
    else:
        proc = subprocess.Popen(
            args,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    return proc.pid


def is_running(pid: int) -> bool:
    try:
        import psutil

        return psutil.pid_exists(pid)
    except ImportError:
        pass
    if current_os() == "windows":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True
        )
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill(pid: int) -> bool:
    try:
        import psutil

        psutil.Process(pid).terminate()
        return True
    except ImportError:
        pass
    except Exception:
        return False
    try:
        if current_os() == "windows":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        else:
            os.kill(pid, 15)
        return True
    except OSError:
        return False


def read_pid(pid_file: Path) -> Optional[int]:
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def pid_create_time(pid: int) -> Optional[float]:
    """Process creation time (epoch seconds) — a PID-reuse guard. None when the
    process is gone or psutil is unavailable (caller falls back to a best-effort
    liveness check)."""
    try:
        import psutil

        return psutil.Process(pid).create_time()
    except ImportError:
        return None
    except Exception:   # NoSuchProcess / AccessDenied / ZombieProcess
        return None


def write_json_atomic(path: Path, obj: Any) -> None:
    """Write JSON without ever leaving a torn file: temp sibling + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
