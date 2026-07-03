"""Search client + daemon control.

Auto-start of the daemon on first query (Genevra pattern: subprocess start,
then retry connect) and — unlike Genevra's daemon-only policy — a graceful
COLD fallback: if the daemon cannot be started (AV, broken env), the query
is answered in-process with a one-off model load and a warning.
"""
from __future__ import annotations

import json
import socket
import sys
import time
from typing import Any, Optional

from ..config import Config
from ..platform import proc

START_TIMEOUT_TRIES = 30   # x1s: model load can take ~10-20s on first run
CONNECT_TIMEOUT = 5.0


def _read_port(cfg: Config) -> Optional[int]:
    try:
        return int(cfg.daemon_port_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _request(port: int, payload: dict[str, Any],
             timeout: float = CONNECT_TIMEOUT) -> Optional[dict[str, Any]]:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.settimeout(max(timeout, 30.0))  # search on a warm model is fast; encode of a long query is not
            s.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
            buf = b""
            while b"\n" not in buf:
                data = s.recv(65536)
                if not data:
                    return None
                buf += data
            return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def ping(cfg: Config) -> Optional[dict[str, Any]]:
    port = _read_port(cfg)
    if port is None:
        return None
    return _request(port, {"cmd": "ping"}, timeout=2.0)


def start_daemon(cfg: Config, wait: bool = True) -> bool:
    """Spawn the daemon detached; return True when it answers ping."""
    if ping(cfg):
        return True
    args = [proc.python_executable(windowed=True), "-m", "singularity",
            "--vault", str(cfg.vault), "daemon", "run"]
    proc.spawn_detached(args, cfg.daemon_log_file)
    if not wait:
        return True
    for _ in range(START_TIMEOUT_TRIES):
        time.sleep(1.0)
        if ping(cfg):
            return True
    return False


def stop_daemon(cfg: Config) -> bool:
    pid = proc.read_pid(cfg.daemon_pid_file)
    if pid is None or not proc.is_running(pid):
        for p in (cfg.daemon_pid_file, cfg.daemon_port_file):
            p.unlink(missing_ok=True)
        return False
    ok = proc.kill(pid)
    time.sleep(0.5)
    return ok


def reload_daemon(cfg: Config) -> Optional[dict[str, Any]]:
    port = _read_port(cfg)
    if port is None:
        return None
    return _request(port, {"cmd": "reload"})


def status(cfg: Config) -> dict[str, Any]:
    pid = proc.read_pid(cfg.daemon_pid_file)
    pong = ping(cfg)
    return {
        "pid": pid,
        "pid_alive": bool(pid and proc.is_running(pid)),
        "port": _read_port(cfg),
        "ping": pong,
    }


# --------------------------------------------------------------------------

def _cold_search(cfg: Config, payload: dict[str, Any]) -> dict[str, Any]:
    print("[mb] WARNING: daemon unavailable — cold one-off search (slow)",
          file=sys.stderr)
    from .engine import SearchEngine

    t0 = time.perf_counter()
    engine = SearchEngine(cfg)
    out = engine.search(payload)
    out["time_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    out["cold"] = True
    return out


def search(cfg: Config, payload: dict[str, Any]) -> dict[str, Any]:
    """Warm search via daemon, auto-starting it; cold fallback as last resort."""
    port = _read_port(cfg)
    if port is not None:
        resp = _request(port, payload)
        if resp is not None:
            return resp
    if start_daemon(cfg):
        port = _read_port(cfg)
        if port is not None:
            resp = _request(port, payload)
            if resp is not None:
                return resp
    return _cold_search(cfg, payload)
