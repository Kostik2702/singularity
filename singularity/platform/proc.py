"""Background process management (daemon spawn/kill), per-OS.

Windows: DETACHED_PROCESS (+pythonw when available).
POSIX:   start_new_session=True (setsid) — the double-fork of the Genevra
         daemon is unnecessary when the parent exits immediately.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import current_os


def python_executable(windowed: bool = False) -> str:
    """Interpreter of the current venv; pythonw.exe for windowed daemons on Windows."""
    exe = sys.executable
    if windowed and current_os() == "windows":
        pythonw = Path(exe).with_name("pythonw.exe")
        if pythonw.exists():
            return str(pythonw)
    return exe


def spawn_detached(args: list[str], log_file: Path) -> int:
    """Start a long-lived background process, return its PID."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
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
