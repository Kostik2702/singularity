"""Run every query of an eval set through a search command and store the raw answers.

The snapshot is the unit of comparison: the scorer (tools/rag_eval.py) never calls the
engine itself, so a baseline frozen today can be scored again tomorrow with a better
judge, and a changed engine is measured by taking a NEW snapshot of the same set.

Usage:
    python tools/rag_eval_snapshot.py --set eval_set.json --out snap.json
    python tools/rag_eval_snapshot.py --set eval_set.json --out snap.json \
        --cmd 'mb search {query} --top {top} --json --full' --tops 5,10 --jobs 4

`--cmd` is split with shlex BEFORE substitution, so {query} becomes exactly one argv
element whatever quotes or spaces it holds. The command must print the `mb search
--json` shape: {"results": [{"meta": {"file","section","content",...}, "score": float}]}.
A non-zero exit or unparsable stdout is recorded per query, never silently dropped.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DEFAULT_CMD = "mb search {query} --top {top} --json --full"
ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))


def vault_path() -> Path:
    """MB_VAULT if set, else the vault this machine's config points at."""
    env = os.environ.get("MB_VAULT")
    if env:
        return Path(env)
    from singularity.config import load_config
    return Path(load_config().vault)


def index_state() -> dict:
    idx = vault_path() / ".mb" / "memory_index"
    try:
        gen = (idx / "CURRENT").read_text().strip()
        summary = json.loads((idx / gen / "summary.json").read_text())
    except OSError as exc:
        return {"error": str(exc)}
    return {"generation": gen, **summary}


def engine_state() -> dict:
    def git(*a):
        r = subprocess.run(["git", "-C", str(ENGINE), *a], capture_output=True, text=True)
        return r.stdout.strip()
    return {"head": git("rev-parse", "HEAD"), "dirty_files": len(git("status", "--porcelain").splitlines())}


def run_one(template: list[str], query: str, top: int, timeout: float) -> dict:
    argv = [part.replace("{query}", query).replace("{top}", str(top)) for part in template]
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"exit": None, "error": "timeout", "elapsed_s": timeout, "results": None}
    elapsed = round(time.perf_counter() - t0, 3)
    rec = {"exit": proc.returncode, "elapsed_s": elapsed, "results": None}
    if proc.returncode != 0:
        rec["error"] = proc.stderr.strip()[-2000:]
        return rec
    try:
        rec["results"] = json.loads(proc.stdout).get("results", [])
    except json.JSONDecodeError as exc:
        rec["error"] = f"unparsable stdout: {exc}"
        rec["stdout_head"] = proc.stdout[:500]
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", required=True,
                    help="eval set JSON; machine-private, lives in the vault's local state, "
                         "e.g. <vault>/.mb/eval/search-eval-v2/eval_set_v2.json")
    ap.add_argument("--out", required=True)
    ap.add_argument("--cmd", default=DEFAULT_CMD)
    ap.add_argument("--tops", default="5,10")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    cases = json.loads(Path(args.set).read_text())["cases"]
    tops = [int(t) for t in args.tops.split(",")]
    template = shlex.split(args.cmd)
    meta = {"label": args.label, "cmd": args.cmd, "tops": tops, "set": str(Path(args.set).resolve()),
            "started": dt.datetime.now().isoformat(timespec="seconds"),
            "index_before": index_state(), "engine": engine_state()}
    jobs = [(c["id"], c["query"], t) for c in cases for t in tops]
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        outs = list(pool.map(lambda j: run_one(template, j[1], j[2], args.timeout), jobs))
    runs: dict[str, dict] = {}
    for (cid, query, top), rec in zip(jobs, outs, strict=True):
        runs.setdefault(cid, {"query": query})[f"top{top}"] = rec
    meta["finished"] = dt.datetime.now().isoformat(timespec="seconds")
    meta["index_after"] = index_state()
    failed = [(cid, k) for cid, r in runs.items() for k, v in r.items() if k.startswith("top") and v["results"] is None]
    meta["failed_runs"] = len(failed)
    Path(args.out).write_text(json.dumps({"meta": meta, "runs": runs}, ensure_ascii=False, indent=1))
    print(f"{len(runs)} queries x {len(tops)} tops -> {args.out}; failed runs: {len(failed)}")
    if meta["index_before"].get("generation") != meta["index_after"].get("generation"):
        print("WARNING: index generation changed during the snapshot", file=sys.stderr)
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
