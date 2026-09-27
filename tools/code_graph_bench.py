"""Timing the code graph against its budgets (CODE-GRAPH D1, budget table).

B1  a verb answered in-process on a warm connection, over the 50 most-called
    symbols (the hubs are where a traversal is expensive): p95 <= 200 ms
B3  refresh of an unchanged tree: overhead over bare `git status` <= 50 ms
B4  <= 200 real edits applied by the query path: <= 2 s
B5  16 callers at once x 100 edits: <= 2 s, 0 stale answers, 0 errors
B6  the walk a unit outside git gets on every query, 100k files: <= 1.2 s
B7  a full build of a 100k-file unit: <= 10 min

Numbers are measured on the SHIPPED extractor and resolver (UNMET i): this
tool calls the same `build_unit` and `query` functions `mb code` calls.

Usage:
    python tools/code_graph_bench.py build --root E:/tmp/code-graph-proxy-100k
    python tools/code_graph_bench.py query --root E:/tmp/code-graph-proxy-100k
    python tools/code_graph_bench.py fresh --root E:/tmp/code-graph-proxy-100k   # edits the tree, then restores it
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _cfg_for(root: Path):
    """A config whose only graph unit is `root`, as an extra root, so a
    benchmark never touches the real repositories root -- with the machine's
    own scope keys, so the graph's scope fingerprint is the one a real `mb`
    query checks it against."""
    from singularity.config import Config, load_config
    real = load_config()
    scope_keys = {k: real.data[k] for k in ("code_skip_dirs", "code_vendored_pattern") if k in real.data}
    return Config(vault=real.vault, data={"code_graph_enabled": True, **scope_keys,
                                          "code_graph_extra_roots": [str(root)]})


def _unit(cfg, root: Path):
    from singularity.codegraph import scope
    units = [u for u in scope.discover_units(cfg) if u.root == root]
    if not units:
        raise SystemExit(f"[bench] {root} did not resolve to a graph unit")
    return units[0]


def cmd_build(root: Path) -> dict:
    import psutil

    from singularity.codegraph import build
    cfg = _cfg_for(root)
    unit = _unit(cfg, root)
    proc = psutil.Process()
    peak = [proc.memory_info().rss]
    import threading
    stop = threading.Event()

    def sample():
        # the build's pool workers included: their RSS is the build's too
        while not stop.is_set():
            total = proc.memory_info().rss
            for child in proc.children(recursive=True):
                try:
                    total += child.memory_info().rss
                except psutil.Error:
                    pass
            peak.append(total)
            time.sleep(0.5)
    t = threading.Thread(target=sample, daemon=True)
    t.start()
    started = time.perf_counter()
    summary = build.build_unit(cfg, unit)
    elapsed = time.perf_counter() - started
    stop.set()
    t.join()
    # Resolve runs on one thread of this process, CPU-bound: a ratio far below
    # 1 means the process sat waiting on something outside it, and the wall
    # time measures the machine, not the code. Seen 2026-09-26: two long
    # background runs at ~0.1-0.3 took 953 s and 649 s; the same build in the
    # foreground, 459 s. (The whole-build ratio says nothing once extraction
    # runs in a pool: this process waits on its workers by design.)
    summary.update(wall_seconds=round(elapsed, 1), peak_rss_mb=round(max(peak) / 2**20),
                   contaminated=summary["resolve_cpu_over_wall"] < 0.8,
                   budget_B7_seconds=600, within_B7=elapsed <= 600)
    return summary


def _pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))], 2)


def cmd_query(root: Path, hubs: int) -> dict:
    from singularity.codegraph import query
    cfg = _cfg_for(root)
    g = query.open_graph(cfg, root)
    top = [r[0] for r in g.con.execute(
        "SELECT dst FROM edges WHERE kind = 'CALLS' GROUP BY dst ORDER BY count(*) DESC LIMIT ?", (hubs,))]
    marks = ",".join("?" * len(top))
    files = [r[0] for r in g.con.execute(
        f"SELECT f.path FROM nodes n JOIN files f ON f.id = n.file_id WHERE n.qname IN ({marks})", top)]
    timings: dict[str, list[float]] = {"callers": [], "callees": [], "trace_d3": [], "outline": []}
    verbs = (("callers", lambda q: query.callers(g.con, q, 50)),
             ("callees", lambda q: query.callees(g.con, q, 50)),
             ("trace_d3", lambda q: query.trace(g.con, q, "callers", 3, 500)))
    for q in top:
        for verb, fn in verbs:
            t = time.perf_counter()
            fn(q)
            timings[verb].append((time.perf_counter() - t) * 1000)
    for f in files:
        t = time.perf_counter()
        query.outline(g.con, f)
        timings["outline"].append((time.perf_counter() - t) * 1000)
    g.con.close()
    allms = [x for xs in timings.values() for x in xs]
    report = {verb: {"n": len(xs), "p50_ms": _pct(xs, 0.5), "p95_ms": _pct(xs, 0.95),
                     "max_ms": round(max(xs), 2), "mean_ms": round(statistics.mean(xs), 2)}
              for verb, xs in timings.items() if xs}
    report["_all"] = {"n": len(allms), "p95_ms": _pct(allms, 0.95), "budget_B1_p95_ms": 200,
                      "within_B1": _pct(allms, 0.95) <= 200}
    return report


def _stats(xs: list[float]) -> dict:
    return {"n": len(xs), "p50_ms": _pct(xs, 0.5), "p95_ms": _pct(xs, 0.95), "max_ms": round(max(xs), 1)}


def _edit(root: Path, paths: list[str], tag: str) -> dict[str, bytes]:
    """A real edit per file: a new function that calls into the file. Returns
    the originals for the revert."""
    saved = {}
    for p in paths:
        f = root / p
        saved[p] = f.read_bytes()
        text = saved[p].decode("utf-8", "replace")
        if p.endswith(".py"):
            text += f"\n\ndef _bench_{tag}():\n    return _bench_{tag}_other()\n"
        else:
            text += f"\nexport function bench_{tag}(): void {{ bench_{tag}_other(); }}\n"
        f.write_bytes(text.encode("utf-8"))
    return saved


def cmd_fresh(root: Path, runs: int) -> dict:
    """B3 (clean-tree refresh), B4 (1/10/100/200 real edits), B5 (16 parallel
    callers x 100 edits), B6 (walk of the whole tree, as outside git)."""
    import os
    import random
    import subprocess

    from singularity.codegraph import fresh, scope, store, update
    os.environ["MB_CODE_NO_BACKGROUND"] = "1"
    cfg = _cfg_for(root)
    unit = _unit(cfg, root)
    key = unit.key
    report: dict = {}

    # B3: an unchanged tree, in process, against bare `git status`
    update.refresh(cfg, key)
    git_ms, ref_ms = [], []
    for _ in range(runs):
        t = time.perf_counter()
        fresh.git_state(root)
        git_ms.append((time.perf_counter() - t) * 1000)
        t = time.perf_counter()
        fr = update.refresh(cfg, key)
        ref_ms.append((time.perf_counter() - t) * 1000)
        assert fr.checked and not fr.updated, fr.as_json()
    report["B3_in_process"] = {"git_status": _stats(git_ms), "refresh": _stats(ref_ms),
                               "overhead_p50_ms": round(_pct(ref_ms, .5) - _pct(git_ms, .5), 1),
                               "budget_overhead_ms": 50}

    # B4: real edits, applied by the query path
    files = [e.path for e in scope.list_files(unit, cfg)]
    rng = random.Random(7)
    b4 = {}
    for n in (1, 10, 100, 200):
        picked = rng.sample(files, n)
        saved = _edit(root, picked, f"b4_{n}")
        t = time.perf_counter()
        fr = update.refresh(cfg, key)
        edit_ms = (time.perf_counter() - t) * 1000
        for p, data in saved.items():
            (root / p).write_bytes(data)
        t = time.perf_counter()
        back = update.refresh(cfg, key)
        revert_ms = (time.perf_counter() - t) * 1000
        b4[n] = {"edit_ms": round(edit_ms), "updated": len(fr.updated), "stale": len(fr.stale_files),
                 "revert_ms": round(revert_ms), "revert_updated": len(back.updated),
                 "revert_stale": len(back.stale_files), "within_2s": edit_ms <= 2000 and revert_ms <= 2000}
        if back.stale_files:
            update.update_unit(cfg, key)
    report["B4"] = b4

    # B5: 16 callers at once after 100 edits; each reports what it saw
    picked = rng.sample(files, 100)
    saved = _edit(root, picked, "b5")
    probe = picked[0]
    script = (
        "import json, os, sys, time\n"
        f"sys.path.insert(0, {str(REPO)!r}); sys.path.insert(0, {str(REPO / 'tools')!r})\n"
        "os.environ['MB_CODE_NO_BACKGROUND'] = '1'\n"
        "from code_graph_bench import _cfg_for\n"
        "from pathlib import Path\n"
        "from singularity.codegraph import store, update\n"
        f"cfg = _cfg_for(Path({str(root)!r}))\n"
        "t = time.perf_counter()\n"
        f"fr = update.refresh(cfg, {key!r})\n"
        "ms = (time.perf_counter() - t) * 1000\n"
        f"con = store.connect_ro(store.current_db(cfg, {key!r}))\n"
        f"h = con.execute('SELECT content_hash FROM files WHERE path = ?', ({probe!r},)).fetchone()[0]\n"
        "print(json.dumps({'ms': ms, 'updated': len(fr.updated), 'stale': len(fr.stale_files), 'hash': h}))\n")
    want = hashlib.sha256((root / probe).read_bytes()).hexdigest()
    t = time.perf_counter()
    procs = [subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) for _ in range(16)]
    outs = [p.communicate(timeout=600) for p in procs]
    wall = (time.perf_counter() - t) * 1000
    errors = [err[-500:] for p, (_, err) in zip(procs, outs, strict=True) if p.returncode != 0]
    seen = [json.loads(out) for p, (out, _) in zip(procs, outs, strict=True) if p.returncode == 0]
    report["B5"] = {"callers": 16, "edits": 100, "wall_ms": round(wall),
                    "per_caller": _stats([s["ms"] for s in seen]) if seen else None,
                    "writers": sum(1 for s in seen if s["updated"]), "errors": len(errors),
                    "stale_answers": sum(1 for s in seen if s["stale"] or s["hash"] != want),
                    "error_tail": errors[:1]}
    for p, data in saved.items():
        (root / p).write_bytes(data)
    update.update_unit(cfg, key)

    # B6: what a unit outside git pays on every query -- the listing walk with
    # its stats, then every stored row compared (the proxy is in git, so the
    # detection's own non-git branch is timed piece by piece)
    # The files B4/B5 rewrote carry racy stats (see store.recorded_mtime). A
    # unit outside git compares every file on every query and writes their
    # settled stats once; do that once here, then time the steady state.
    time.sleep(store.RACY_NS / 1e9 + 0.5)
    wanted = {e.path for e in scope.list_files(unit, cfg)}
    con = store.connect_ro(store.current_db(cfg, key))
    meta = store.read_meta(con)
    settle = fresh.Detection(head=meta.get("indexed_head"),
                             dirty={r[0] for r in con.execute("SELECT path FROM dirty")})
    found, _ = scope.walk_stat(root, scope._skip_dirs(cfg), scope._vendored(cfg))
    fresh._compare_all(root, con, {p: st for p, st in found.items() if p in wanted}, settle)
    con.close()
    update.apply(store.current_db(cfg, key), settle, meta.get("seq", 0), cap_dependents=False)
    walk_ms = []
    con = store.connect_ro(store.current_db(cfg, key))
    for _ in range(min(runs, 5)):
        t = time.perf_counter()
        found, _ = scope.walk_stat(root, scope._skip_dirs(cfg), scope._vendored(cfg))
        det = fresh.Detection()
        fresh._compare_all(root, con, {p: st for p, st in found.items() if p in wanted}, det)
        assert not det.changes, sorted(det.changes)[:5]
        walk_ms.append((time.perf_counter() - t) * 1000)
    report["B6_walk_and_compare"] = {**_stats(walk_ms), "files_walked": len(found), "budget_ms": 1200}
    report["graph"] = {"files": con.execute("SELECT count(*) FROM files").fetchone()[0]}
    con.close()
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("build", "query", "fresh"):
        p = sub.add_parser(name)
        p.add_argument("--root", required=True)
        p.add_argument("--out")
        if name == "query":
            p.add_argument("--hubs", type=int, default=50)
        if name == "fresh":
            p.add_argument("--runs", type=int, default=20)
    args = ap.parse_args(argv)
    root = Path(args.root)
    result = (cmd_build(root) if args.cmd == "build" else cmd_query(root, args.hubs) if args.cmd == "query"
              else cmd_fresh(root, args.runs))
    text = json.dumps(result, indent=1, default=str)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
