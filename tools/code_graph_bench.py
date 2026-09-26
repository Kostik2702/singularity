"""Timing the code graph against its budgets (CODE-GRAPH D1, budget table).

B1  a verb answered in-process on a warm connection, over the 50 most-called
    symbols (the hubs are where a traversal is expensive): p95 <= 200 ms
B7  a full build of a 100k-file unit: <= 10 min

Numbers are measured on the SHIPPED extractor and resolver (UNMET i): this
tool calls the same `build_unit` and `query` functions `mb code` calls.

Usage:
    python tools/code_graph_bench.py build --root E:/tmp/code-graph-proxy-100k
    python tools/code_graph_bench.py query --root E:/tmp/code-graph-proxy-100k
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _cfg_for(root: Path):
    """A config whose only graph unit is `root`, as an extra root, so a
    benchmark never touches the real repositories root."""
    from singularity.config import Config, load_config
    real = load_config()
    return Config(vault=real.vault, data={"code_graph_enabled": True,
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
        while not stop.is_set():
            peak.append(proc.memory_info().rss)
            time.sleep(0.5)
    t = threading.Thread(target=sample, daemon=True)
    t.start()
    started, cpu_started = time.perf_counter(), time.process_time()
    summary = build.build_unit(cfg, unit)
    elapsed = time.perf_counter() - started
    cpu_over_wall = (time.process_time() - cpu_started) / elapsed
    stop.set()
    t.join()
    # The build is one CPU-bound thread: a ratio far below 1 means the process
    # sat waiting on something outside it, and the wall time measures the
    # machine, not the code. Seen 2026-09-26: two long background runs at
    # ~0.1-0.3 took 953 s and 649 s; the same build in the foreground, 459 s.
    summary.update(wall_seconds=round(elapsed, 1), peak_rss_mb=round(max(peak) / 2**20),
                   cpu_over_wall=round(cpu_over_wall, 2), contaminated=cpu_over_wall < 0.8,
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("build", "query"):
        p = sub.add_parser(name)
        p.add_argument("--root", required=True)
        p.add_argument("--out")
        if name == "query":
            p.add_argument("--hubs", type=int, default=50)
    args = ap.parse_args(argv)
    root = Path(args.root)
    result = cmd_build(root) if args.cmd == "build" else cmd_query(root, args.hubs)
    text = json.dumps(result, indent=1, default=str)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
