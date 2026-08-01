"""Warm-model search daemon: TCP 127.0.0.1, newline-delimited JSON.

Commands: {"cmd":"ping"} | {"cmd":"reload"} | {"cmd":"search", ...} |
{"cmd":"index", "action":"update|rebuild", "files":[...]}
(the Genevra reload bug `command` vs `cmd` is fixed here: only `cmd`).

Loopback-only bind; actual port is written to .mb/daemon.port so the client
never guesses (config daemon_port=0 -> OS picks a free one).
"""
from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time
import uuid
from datetime import datetime
from typing import Any

from ..config import Config
from ..platform import proc
from .engine import SearchEngine

MAX_LINE = 1_000_000


class SearchDaemon:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.engine: SearchEngine | None = None
        self._running = True
        self.instance_id = uuid.uuid4().hex

    # ------------------------------------------------------------------
    def _handle(self, req: dict[str, Any]) -> dict[str, Any]:
        cmd = req.get("cmd", "search")
        if cmd == "ping":
            return {"status": "ok", "pid": os.getpid(), "instance_id": self.instance_id}
        if cmd == "reload":
            counts = self.engine.reload()
            return {"status": "reloaded", **counts}
        if cmd == "search":
            t0 = time.perf_counter()
            out = self.engine.search(req)
            out["time_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            return out
        if cmd == "index":
            from . import indexer

            action = req.get("action")
            files = req.get("files")
            corpus = req.get("corpus") or "memory"
            if action not in {"update", "rebuild"}:
                return {"error": f"unknown index action: {action}"}
            if corpus not in indexer.CORPUS_BUILDERS:
                return {"error": f"unknown corpus: {corpus}"}
            if corpus == "sources" and not self.cfg.get("ingest_index_sources"):
                # single gate for every remote path (CLI, nightly, add catch-up)
                return {"error": "sources indexing is disabled "
                                 "(config ingest_index_sources=false)",
                        "error_type": "SourcesIndexDisabled"}
            if corpus == "code" and self.cfg.code_repos_dir is None:
                # same shape as the sources gate: the CLI checks this too, but
                # the daemon is the only choke point EVERY caller passes through
                return {"error": "the code corpus is off (config code_repos_dir "
                                 "is not set)",
                        "error_type": "CodeCorpusNotConfigured"}
            if files is not None and (not isinstance(files, list)
                                      or not all(isinstance(f, str) for f in files)):
                return {"error": "index files must be a list of vault-relative strings"}
            try:
                stats = (indexer.rebuild(self.cfg, corpus=corpus) if action == "rebuild"
                         else indexer.update(self.cfg, only_files=files or None,
                                             corpus=corpus))
            except indexer.IndexLockedError as exc:
                return {"error": str(exc), "error_type": "IndexLockedError"}
            except indexer.CorpusRootUnavailable as exc:
                # the index was deliberately left untouched — this is a refusal,
                # not a failed write
                return {"error": str(exc), "error_type": "CorpusRootUnavailable"}
            counts = self.engine.reload()
            return {"status": "indexed", "stats": stats, "index": counts,
                    "corpus": corpus}
        return {"error": f"unknown cmd: {cmd}"}

    def _serve_conn(self, conn: socket.socket) -> None:
        conn.settimeout(10.0)
        try:
            buf = b""
            while b"\n" not in buf:
                data = conn.recv(65536)
                if not data:
                    return
                buf += data
                if len(buf) > MAX_LINE:
                    conn.sendall(b'{"error":"request too large"}\n')
                    return
            line = buf.split(b"\n", 1)[0]
            try:
                req = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                conn.sendall(b'{"error":"Invalid JSON"}\n')
                return
            try:
                resp = self._handle(req)
            except Exception as exc:  # noqa: BLE001 — daemon must not die on one request
                resp = {"error": str(exc)}
            conn.sendall(json.dumps(resp, ensure_ascii=False).encode("utf-8") + b"\n")
        except socket.timeout:
            pass
        finally:
            conn.close()

    # ------------------------------------------------------------------
    def run(self) -> None:
        cfg = self.cfg
        cfg.mb_dir.mkdir(parents=True, exist_ok=True)
        print(f"[daemon] loading model {cfg.model} ...", flush=True)
        t0 = time.perf_counter()
        self.engine = SearchEngine(cfg)
        print(f"[daemon] model+index ready in {time.perf_counter()-t0:.1f}s "
              f"(memory={len(self.engine.mem_chunks)}, code={len(self.engine.code_chunks)})",
              flush=True)

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", int(cfg.get("daemon_port", 0))))
        srv.listen(5)
        srv.settimeout(1.0)  # so the shutdown flag is polled
        port = srv.getsockname()[1]
        # Atomic identity state written AFTER bind, so a reader never sees a
        # port without the identity that owns it. Legacy port/pid files are
        # kept for backward compatibility.
        pid = os.getpid()
        from ..config import machine_id
        proc.write_json_atomic(cfg.daemon_state_file, {
            "instance_id": self.instance_id,
            "pid": pid,
            "port": port,
            "create_time": proc.pid_create_time(pid),
            "python": sys.executable,
            "machine_id": machine_id(),
            "started_at": datetime.now().isoformat(timespec="seconds"),
        })
        cfg.daemon_port_file.write_text(str(port), encoding="utf-8")
        cfg.daemon_pid_file.write_text(str(pid), encoding="utf-8")
        print(f"[daemon] listening on 127.0.0.1:{port}", flush=True)

        def _stop(*_a):
            self._running = False

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        try:
            while self._running:
                try:
                    conn, _addr = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                self._serve_conn(conn)
        finally:
            srv.close()
            self._cleanup_state(cfg, pid, self.instance_id)
            print("[daemon] stopped", flush=True)

    # ------------------------------------------------------------------
    @staticmethod
    def _cleanup_state(cfg: Config, own_pid: int,
                       own_instance_id: str | None = None) -> None:
        """Remove the state files ONLY while they still record THIS instance.

        The shutdown flag is polled between requests, so a daemon that is
        mid-request (an in-daemon `{"cmd":"index"}` rebuild holds `_serve_conn`
        for minutes) outlives its own SIGTERM by that long. By the time it gets
        here a replacement may already have bound a port and written its own
        identity — unlinking unconditionally would delete the *successor's*
        state and leave a live-but-unreachable daemon behind (the starter's
        ping loop then times out and reports a bogus startup failure).
        """
        try:
            state = json.loads(cfg.daemon_state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = None
        if isinstance(state, dict):
            state_iid = state.get("instance_id")
            state_pid = state.get("pid")
            if own_instance_id is not None and isinstance(state_iid, str):
                if state_iid != own_instance_id:
                    return          # a newer daemon owns the state now
            elif state_pid is not None and state_pid != own_pid:
                return              # legacy state without instance_id — pid wins
        for p in (cfg.daemon_state_file, cfg.daemon_port_file, cfg.daemon_pid_file):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def main(cfg: Config) -> None:
    SearchDaemon(cfg).run()
