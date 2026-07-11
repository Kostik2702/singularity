"""Search client + daemon control.

Auto-start of the daemon on first query (Genevra pattern: subprocess start,
then retry connect) and — unlike Genevra's daemon-only policy — a graceful
COLD fallback: if the daemon cannot be started (AV, broken env), the query
is answered in-process with a one-off model load and a warning.
"""
from __future__ import annotations

import json
import shutil
import socket
import sys
import time
from typing import Any, Optional

from ..config import Config
from ..platform import proc

START_TIMEOUT_TRIES = 30   # x1s: model load can take ~10-20s on first run
CONNECT_TIMEOUT = 5.0


def _read_state(cfg: Config) -> Optional[dict[str, Any]]:
    try:
        return json.loads(cfg.daemon_state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_port(cfg: Config) -> Optional[int]:
    state = _read_state(cfg)
    if state and isinstance(state.get("port"), int):
        return state["port"]
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


def _clear_state(cfg: Config) -> None:
    for p in (cfg.daemon_state_file, cfg.daemon_pid_file, cfg.daemon_port_file):
        p.unlink(missing_ok=True)


def start_daemon(cfg: Config, wait: bool = True) -> bool:
    """Spawn the daemon detached (singleton — serialized by a start lock so two
    concurrent starts cannot both spawn); return True when it answers ping."""
    if ping(cfg):
        return True
    lock = cfg.daemon_start_lock
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir(exist_ok=False)
        owns_lock = True
    except FileExistsError:
        # another starter is mid-spawn — don't spawn a second; wait for its ping
        owns_lock = False
        if time.time() - lock.stat().st_mtime > 120:
            _reap_lock(lock)                 # abandoned start lock -> take over
            try:
                lock.mkdir(exist_ok=False)
                owns_lock = True
            except FileExistsError:
                owns_lock = False
    try:
        if owns_lock:
            if ping(cfg):                    # someone won the race before us
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
    finally:
        if owns_lock:
            _reap_lock(lock)


def _reap_lock(lock) -> None:
    shutil.rmtree(lock, ignore_errors=True)


def stop_daemon(cfg: Config) -> bool:
    """Stop the daemon, refusing to kill a PID that has been reused by an
    unrelated process (identity = recorded process creation time)."""
    state = _read_state(cfg)
    # foreign-machine guard: never touch a daemon recorded by another machine
    # that syncs this vault (AUD-19). With per-machine state dirs this is belt-
    # and-suspenders, but a legacy shared state file could still be foreign.
    if state and state.get("machine_id"):
        from ..config import machine_id
        if state["machine_id"] != machine_id():
            return False
    pid = state.get("pid") if state else proc.read_pid(cfg.daemon_pid_file)
    if pid is None or not proc.is_running(pid):
        _clear_state(cfg)
        return False
    # PID-reuse guard: if we recorded a creation time, the live process must
    # match it; otherwise this PID now belongs to something else — never kill it.
    recorded = (state or {}).get("create_time")
    if recorded is not None:
        live = proc.pid_create_time(pid)
        if live is not None and live != recorded:
            _clear_state(cfg)                # stale identity, not our daemon
            return False
    ok = proc.kill(pid)
    time.sleep(0.5)
    if ok:
        _clear_state(cfg)
    return ok


def reload_daemon(cfg: Config) -> Optional[dict[str, Any]]:
    port = _read_port(cfg)
    if port is None:
        return None
    return _request(port, {"cmd": "reload"})


def status(cfg: Config) -> dict[str, Any]:
    state = _read_state(cfg)
    pid = state.get("pid") if state else proc.read_pid(cfg.daemon_pid_file)
    pong = ping(cfg)
    pid_alive = bool(pid and proc.is_running(pid))
    # orphan = we have recorded state but the process is gone (crashed daemon)
    orphan = bool(state and not pid_alive)
    return {
        "pid": pid,
        "pid_alive": pid_alive,
        "port": _read_port(cfg),
        "instance_id": (state or {}).get("instance_id"),
        "orphan": orphan,
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
