"""mb selftest — end-to-end pipeline check (AUD-16).

Creates a unique throwaway note in a dedicated `_selftest/` folder, indexes it,
finds it by a unique token through the search path, deletes it, and confirms it
disappears from the index — then removes every artifact, even on interrupt. It
never touches the user's canonical notes. Human output by default; --json emits
one machine-readable document.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any
from collections.abc import Callable

from ..config import Config

SELFTEST_DIR = "_selftest"


def _token() -> str:
    # unique without Math.random/Date: pid + monotonic-derived counter is enough
    return f"mbselftk{os.getpid()}{int(time.time() * 1000) % 1_000_000:06d}"


def run(cfg: Config, as_json: bool = False) -> int:
    from ..search import client

    token = _token()
    rel = f"{SELFTEST_DIR}/{token}.md"
    note = cfg.vault / SELFTEST_DIR / f"{token}.md"
    stages: list[dict[str, Any]] = []
    ok = True

    def stage(name: str, fn: Callable[[], str]) -> bool:
        nonlocal ok
        t0 = time.perf_counter()
        try:
            detail = fn()
            status = "PASS"
        except Exception as exc:  # noqa: BLE001 — report, don't crash the runner
            detail, status, ok = f"{type(exc).__name__}: {exc}", "FAIL", False
        stages.append({"stage": name, "status": status, "detail": detail,
                       "ms": round((time.perf_counter() - t0) * 1000, 1)})
        return status == "PASS"

    def _found() -> bool:
        # The query carries the note's own wording, not just the opaque token,
        # and is scoped to the throwaway folder. Searching for a random token
        # alone made this a SEMANTIC search for a string with no semantics: the
        # embedding of `mbselftk34444505896` is near-noise, so as the vault grew
        # the note's own chunk stopped reaching the top-5 and the gate started
        # failing on a healthy system (observed at ~3.2k chunks, passing at 3216
        # and failing at 3244). Scoping also widens the candidate pool, since
        # `_search_source` uses top_k*20 once any filter is present.
        resp = client.search(cfg, {
            "cmd": "search",
            "query": f"selftest probe unique token {token}",
            "path_prefix": f"{SELFTEST_DIR}/", "top_k": 5, "format": "json"})
        return any(r.get("file") == rel or token in json.dumps(r, ensure_ascii=False)
                   for r in resp.get("results", []))

    try:
        # Stages nest on purpose: each runs only if the previous one
        # succeeded, and the lambdas are too long to collapse (SIM102).
        if stage("create", lambda: (  # noqa: SIM102
                note.parent.mkdir(parents=True, exist_ok=True),
                note.write_text(f"# selftest\n\n## probe\n\nUnique token {token} "
                                "for the mb selftest end-to-end check.\n",
                                encoding="utf-8"),
                f"wrote {rel}")[-1]):
            # Nested on purpose: each stage runs only if the previous one
            # succeeded, and the lambdas are too long to collapse (SIM102).
            if stage("index", lambda: f"indexed: {client.run_index(cfg, 'update', [rel])}"):
                stage("find", lambda: "token found in index"
                      if _found() else _fail("token NOT found after indexing"))
        stage("delete", lambda: (note.unlink(missing_ok=True),
                                 f"removed, reindex: {client.run_index(cfg, 'update', [rel])}")[-1])
        stage("verify-gone", lambda: "token gone from index"
              if not _found() else _fail("token still present after delete"))
    finally:
        # cleanup always — even on KeyboardInterrupt during a stage
        try:
            note.unlink(missing_ok=True)
            d = cfg.vault / SELFTEST_DIR
            if d.exists() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass

    payload = {"ok": ok, "token": token, "stages": stages}
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        for s in stages:
            print(f"[{s['status']}] {s['stage']} ({s['ms']}ms): {s['detail']}")
        print("selftest:", "OK" if ok else "FAILED")
    return 0 if ok else 1


def _fail(msg: str) -> str:
    raise RuntimeError(msg)
