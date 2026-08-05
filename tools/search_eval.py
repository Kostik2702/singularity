"""Recall measurement for `mb search` against a fixed query -> note set.

Why this exists: `singularity/search/lexical.py` cites an evaluation set as the
reason BM25's k1/b and RRF's k are not exposed as config -- "a change it cannot
justify should not be made". That set is `tests/fixtures/search_eval.json`, and
until this tool existed the citation had nothing behind it. Every proposal that
touches ranking (turn hybrid on by default, boost by usage, swap the model) is
decided here or not at all.

The vault is LIVE, so absolute numbers drift as notes are added. What is
comparable is a SAME-SESSION pair: one vault, one model, one variable. That is
what `--hybrid both` produces, and it is the only evidence a ranking change
should be argued from.

A missing target note is a FAILURE (exit 2), never a skipped case. A set that
quietly shrinks when the vault moves under it would report a rising score for
the wrong reason -- the exact shape of "green because it measured nothing".

Usage:
    python tools/search_eval.py run                       # current config
    python tools/search_eval.py run --hybrid both         # the comparable pair
    python tools/search_eval.py run --out docs/search-eval-baseline.json
    python tools/search_eval.py compare old.json new.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

FIXTURE = REPO / "tests" / "fixtures" / "search_eval.json"
CUTOFFS = (1, 5, 10)
TOP_K = 10          # deepest cutoff; asking for more would measure a longer tail
                    # than any caller actually reads.


# --- metrics: pure, so the test suite can gate them without a vault ---------

def ranked_files(results: list[dict[str, Any]]) -> list[str]:
    """Result chunks collapsed to a ranking of FILES, first occurrence wins.

    Recall here is a question about notes, not chunks: three chunks of one note
    at ranks 1-3 have found one note, and counting them as three would make a
    verbose note look like a better answer than a precise one.
    """
    seen: list[str] = []
    for item in results:
        meta = item.get("meta") or {}
        path = meta.get("file")
        if isinstance(path, str) and path and path not in seen:
            seen.append(path)
    return seen


def first_hit_rank(ranking: list[str], expected: list[str]) -> int | None:
    """1-based rank of the first expected file, or None if it never appears."""
    wanted = set(expected)
    for position, path in enumerate(ranking, 1):
        if path in wanted:
            return position
    return None


def score_cases(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-case ranks into recall@k and MRR, overall and per class.

    `rows` is [{"class": str, "rank": int|None}, ...]; a None rank is a miss,
    which contributes 0 to every cutoff and 0 to MRR.
    """
    buckets: dict[str, list[int | None]] = defaultdict(list)
    for row in rows:
        buckets[row["class"]].append(row["rank"])
        buckets["_overall"].append(row["rank"])

    out: dict[str, Any] = {}
    for name, ranks in buckets.items():
        total = len(ranks)
        entry: dict[str, Any] = {"n": total}
        for k in CUTOFFS:
            hits = sum(1 for r in ranks if r is not None and r <= k)
            entry[f"recall@{k}"] = round(hits / total, 4) if total else 0.0
        mrr = sum((1.0 / r) for r in ranks if r is not None)
        entry["mrr"] = round(mrr / total, 4) if total else 0.0
        entry["misses"] = sum(1 for r in ranks if r is None)
        out[name] = entry
    return out


# --- running against the live vault ----------------------------------------

def load_fixture(path: Path = FIXTURE) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def verify_targets(cases: list[dict[str, Any]], vault: Path) -> list[str]:
    """Every expected note must exist. Returns the missing ones."""
    missing = []
    for case in cases:
        for rel in case["expect"]:
            if not (vault / rel).exists():
                missing.append(f"{case['id']} -> {rel}")
    return missing


def run_once(cases: list[dict[str, Any]], hybrid: bool | None,
             corpus: str = "memory") -> dict[str, Any]:
    from singularity.config import load_config
    from singularity.search import client

    cfg = load_config()
    code = corpus == "code"
    rows, per_case = [], []
    started = time.time()
    for case in cases:
        payload = {
            "cmd": "search", "query": case["query"], "top_k": TOP_K,
            "hybrid": hybrid, "memory_only": not code, "code_only": code,
            "include_code": code, "include_sources": False,
            "format": "json",
            # min_score is deliberately NOT overridden: the number this tool
            # reports must be the recall a real `mb search` would deliver.
        }
        resp = client.search(cfg, payload)
        if "error" in resp:
            raise SystemExit(f"[eval] search failed on {case['id']}: {resp['error']}")
        ranking = ranked_files(resp.get("results") or [])
        rank = first_hit_rank(ranking, case["expect"])
        rows.append({"class": case["class"], "rank": rank})
        per_case.append({
            "id": case["id"], "class": case["class"], "query": case["query"],
            "rank": rank, "expect": case["expect"],
            "got": ranking[:3],
        })
    return {
        "hybrid": hybrid,
        "corpus": corpus,
        "elapsed_s": round(time.time() - started, 2),
        "metrics": score_cases(rows),
        "cases": per_case,
    }


def render(run: dict[str, Any], *, show_misses: bool = True) -> None:
    label = {True: "on", False: "off", None: "config"}[run["hybrid"]]
    corpus = run.get("corpus", "memory")
    m = run["metrics"]
    print(f"== corpus={corpus} hybrid={label}  ({run['elapsed_s']}s) ==")
    header = f"{'class':<14}{'n':>4}" + "".join(f"{'R@'+str(k):>9}" for k in CUTOFFS) + f"{'MRR':>9}{'miss':>6}"
    print(header)
    order = [c for c in ("identifier", "natural", "crosslingual") if c in m] + ["_overall"]
    for name in order:
        e = m[name]
        cells = "".join(f"{e['recall@'+str(k)]:>9.3f}" for k in CUTOFFS)
        print(f"{name:<14}{e['n']:>4}{cells}{e['mrr']:>9.3f}{e['misses']:>6}")
    if show_misses:
        misses = [c for c in run["cases"] if c["rank"] is None]
        if misses:
            print(f"\n-- not found in top {TOP_K} ({len(misses)}) --")
            for c in misses:
                print(f"   [{c['class']}] {c['id']}  {c['query']}")
                print(f"       want: {c['expect'][0]}")
                print(f"       got : {c['got'][0] if c['got'] else '(nothing)'}")


def render_delta(a: dict[str, Any], b: dict[str, Any]) -> None:
    """b relative to a, per class. Sign is what the decision reads."""
    print(f"\n== delta: hybrid={b['hybrid']} minus hybrid={a['hybrid']} ==")
    print(f"{'class':<14}" + "".join(f"{'R@'+str(k):>9}" for k in CUTOFFS) + f"{'MRR':>9}")
    order = [c for c in ("identifier", "natural", "crosslingual") if c in b["metrics"]] + ["_overall"]
    for name in order:
        ea, eb = a["metrics"][name], b["metrics"][name]
        cells = "".join(f"{eb['recall@'+str(k)] - ea['recall@'+str(k)]:>+9.3f}" for k in CUTOFFS)
        print(f"{name:<14}{cells}{eb['mrr'] - ea['mrr']:>+9.3f}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="measure recall against the live vault")
    r.add_argument("--hybrid", choices=("on", "off", "both", "config"), default="config")
    r.add_argument("--corpus", choices=("memory", "code"), default="memory",
                   help="which corpus to measure; scored separately because the "
                        "query shapes differ and averaging would hide which one moved")
    r.add_argument("--out", help="write the run(s) as JSON")
    r.add_argument("--quiet", action="store_true", help="metrics only, no miss list")

    c = sub.add_parser("compare", help="delta between two saved runs")
    c.add_argument("before")
    c.add_argument("after")

    args = ap.parse_args(argv)

    if args.cmd == "compare":
        a = json.loads(Path(args.before).read_text(encoding="utf-8"))
        b = json.loads(Path(args.after).read_text(encoding="utf-8"))
        for run in (a, b):
            render(run, show_misses=False)
        render_delta(a, b)
        return 0

    from singularity.config import load_config
    fixture = load_fixture()
    cases = fixture["code_cases"] if args.corpus == "code" else fixture["cases"]

    cfg = load_config()
    # Code targets live under the repositories root, not the vault.
    root = Path(cfg.code_repos_dir) if args.corpus == "code" else Path(cfg.vault)
    missing = verify_targets(cases, root)
    if missing:
        print(f"[eval] {len(missing)} expected note(s) missing from the vault -- "
              f"the set measures nothing until these are fixed:", file=sys.stderr)
        for m in missing:
            print(f"   {m}", file=sys.stderr)
        return 2

    modes = {"on": [True], "off": [False], "config": [None], "both": [False, True]}[args.hybrid]
    runs = []
    for mode in modes:
        run = run_once(cases, mode, corpus=args.corpus)
        runs.append(run)
        render(run, show_misses=not args.quiet)
        print()
    if len(runs) == 2:
        render_delta(runs[0], runs[1])

    if args.out:
        payload = runs[0] if len(runs) == 1 else {"runs": runs}
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        print(f"\n[eval] wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
