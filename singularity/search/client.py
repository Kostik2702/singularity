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
from typing import Any

from ..config import Config
from ..platform import proc

START_TIMEOUT_TRIES = 30   # x1s: model load can take ~10-20s on first run
CONNECT_TIMEOUT = 5.0
INDEX_READ_TIMEOUT = 60 * 30.0


class RemoteIndexError(RuntimeError):
    def __init__(self, message: str, *, error_type: str | None = None):
        super().__init__(message)
        self.error_type = error_type


def _read_state(cfg: Config) -> dict[str, Any] | None:
    try:
        return json.loads(cfg.daemon_state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_port(cfg: Config) -> int | None:
    state = _read_state(cfg)
    if state and isinstance(state.get("port"), int):
        return state["port"]
    try:
        return int(cfg.daemon_port_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _request(port: int, payload: dict[str, Any],
             timeout: float = CONNECT_TIMEOUT,
             read_timeout: float | None = None) -> dict[str, Any] | None:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.settimeout(read_timeout if read_timeout is not None
                         else max(timeout, 30.0))
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


def ping(cfg: Config) -> dict[str, Any] | None:
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
            # never the caller's CWD: `mb daemon start` run one level above the
            # clone would spawn a daemon that dies on a shadowed import and
            # report only "FAILED to start" 30s later (issue #16)
            proc.spawn_detached(args, cfg.daemon_log_file,
                                cwd=proc.module_search_root())
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


def reload_daemon(cfg: Config) -> dict[str, Any] | None:
    port = _read_port(cfg)
    if port is None:
        return None
    return _request(port, {"cmd": "reload"})


def restart_daemon(cfg: Config) -> bool:
    """Stop then start. The remedy for anything `reload` cannot re-bind — a
    changed embedding model, most of all — had no single command before
    (issue #12), and `reload`, the one that SOUNDS like it, was the one that
    did not do it."""
    stop_daemon(cfg)
    return start_daemon(cfg)


def daemon_value(cfg: Config, key: str) -> tuple[bool, Any]:
    """What the RUNNING daemon currently serves for `key`.

    Returns (daemon_answered, value). A daemon that predates this command
    answers with an error, which is reported as "did not answer" rather than as
    a mismatch — an old daemon must not produce a scary warning."""
    port = _read_port(cfg)
    if port is None:
        return False, None
    try:
        resp = _request(port, {"cmd": "config", "key": key})
    except Exception:                       # noqa: BLE001 — advisory path only
        return False, None
    if not isinstance(resp, dict) or "value" not in resp:
        return False, None
    return True, resp["value"]


def run_index(cfg: Config, action: str = "update",
              files: list[str] | None = None,
              corpus: str | None = None) -> dict[str, Any]:
    """Run an index write inside the warm daemon process.

    There is deliberately no local fallback: a one-off CLI process would load
    another copy of the embedding model and defeat the daemon-resident contract.

    `corpus` selects which body to index (W03); omitted == the vault corpus,
    so every pre-W03 call site keeps its exact behavior.
    """
    if action not in {"update", "rebuild"}:
        raise ValueError(f"unknown index action: {action}")
    payload: dict[str, Any] = {"cmd": "index", "action": action}
    if files:
        payload["files"] = files
    if corpus:
        payload["corpus"] = corpus
    port = _read_port(cfg)
    resp = (_request(port, payload, read_timeout=INDEX_READ_TIMEOUT)
            if port is not None else None)
    if resp is None:
        if not start_daemon(cfg):
            raise RemoteIndexError(
                "search daemon unavailable; run `mb daemon start` and inspect the daemon log",
                error_type="DaemonUnavailable")
        port = _read_port(cfg)
        if port is None:
            raise RemoteIndexError("search daemon started without a readable port",
                                   error_type="DaemonUnavailable")
        resp = _request(port, payload, read_timeout=INDEX_READ_TIMEOUT)
    if resp is None:
        raise RemoteIndexError("index daemon request failed or timed out",
                               error_type="DaemonUnavailable")
    if resp.get("error"):
        raise RemoteIndexError(str(resp["error"]),
                               error_type=resp.get("error_type"))
    stats = resp.get("stats")
    if not isinstance(stats, dict):
        raise RemoteIndexError("index daemon returned an invalid response")
    return stats


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
