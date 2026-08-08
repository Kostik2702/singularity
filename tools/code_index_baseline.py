"""Control-query baseline for the CODE-INDEX epic (C00) and its re-run (C06).

Why a tool and not ten hand-run commands: decision D2 (`--with-code` is opt-in)
is explicitly re-openable "by the numbers of C06". Numbers you cannot reproduce
are an opinion, so the same ten queries must be replayable months later against
a vault that has meanwhile grown.

The vault is LIVE. Absolute scores drift the moment a note is added, so the
authoritative evidence for D2 is the SAME-SESSION pair produced by
`capture --both`: identical vault, identical model, one variable (`--with-code`).
The C00 snapshot committed under docs/ is the historical record of "before the
code corpus existed at all", not a byte-comparison target.

Usage:
    python tools/code_index_baseline.py capture --out docs/code-index-baseline.json
    python tools/code_index_baseline.py capture --both --out /tmp/pair.json
    python tools/code_index_baseline.py render docs/code-index-baseline.json
    python tools/code_index_baseline.py compare /tmp/pair.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent

# Ten REAL questions to the memory, not synthetic probes: each one was actually
# asked (or is the shape of what gets asked) during Singularity work. They were
# re-worded into English when the engine was standardised on one language, so a
# capture taken before that cannot be read with these — `_reject_a_foreign_query_set`
# refuses it rather than pairing by id. The `kind`
# column is the note type the query is expected to land on -- it documents the
# coverage claim (decision / howto / incident / reference / rule / learning),
# and a drift in `kind` is itself a signal worth reading.
QUERIES: list[dict[str, str]] = [
    {"id": "Q01", "kind": "decision",
     "query": "why is the index rebuilt in full"},
    {"id": "Q02", "kind": "howto",
     "query": "how to add a new corpus to semantic search"},
    {"id": "Q03", "kind": "incident",
     "query": "Glob on Windows silently returns an empty result"},
    {"id": "Q04", "kind": "reference",
     "query": "atomic commit of index generations and torn-write protection"},
    {"id": "Q05", "kind": "incident",
     "query": "TLS MITM certificate when downloading a model from Hugging Face"},
    {"id": "Q06", "kind": "rule",
     "query": "what must be written to memory after a task"},
    {"id": "Q07", "kind": "reference",
     "query": "how the nightly run works and which steps it has"},
    {"id": "Q08", "kind": "decision",
     "query": "is a watcher over code repositories needed"},
    {"id": "Q09", "kind": "reference",
     "query": "markdown chunking overlapping windows and minimum chunk size"},
    {"id": "Q10", "kind": "learning",
     "query": "why search answers slowly and what the cold fallback is"},
]

TOP_K = 5


def _mb(args: list[str]) -> dict[str, Any]:
    """Invoke the CLI through the *current* interpreter so the snapshot can
    never be taken against a different install than the one under test."""
    proc = subprocess.run(
        [sys.executable, "-m", "singularity", *args],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8",
        errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"mb {' '.join(args)} -> exit {proc.returncode}\n"
                           f"{proc.stderr.strip()}")
    return json.loads(proc.stdout)


def _hit(row: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("meta", {})
    return {
        "file": meta.get("file"),
        "chunk_id": meta.get("chunk_id"),
        "score": row.get("score"),
        "source": row.get("source"),
        "type": meta.get("type"),
        "kind": meta.get("kind"),
        "name": meta.get("name"),
        "repo": meta.get("repo"),
        "section": meta.get("section"),
    }


def _run_variant(with_code: bool) -> list[dict[str, Any]]:
    rows = []
    for spec in QUERIES:
        args = ["search", spec["query"], "--top", str(TOP_K), "--json"]
        if with_code:
            args.append("--with-code")
        t0 = time.perf_counter()
        resp = _mb(args)
        wall_ms = round((time.perf_counter() - t0) * 1000, 1)
        rows.append({
            **spec,
            "top": [_hit(r) for r in resp.get("results", [])],
            "engine_ms": resp.get("time_ms"),
            "wall_ms": wall_ms,
            "cold": bool(resp.get("cold")),
            "warnings": {k: v for k, v in resp.items() if k.endswith("_warning")},
        })
    return rows


def _stats() -> dict[str, Any]:
    try:
        return _mb(["stats", "--json"])
    except (RuntimeError, json.JSONDecodeError) as exc:
        return {"error": str(exc)}


def cmd_capture(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {
        "taken_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "top_k": TOP_K,
        "note": args.note or "",
        "stats": _stats(),
        "variants": {},
    }
    # warm the daemon first so the recorded timings are the warm path, which is
    # the only one a user experiences twice
    _mb(["search", "warmup", "--top", "1", "--json"])
    payload["variants"]["baseline"] = _run_variant(with_code=False)
    if args.both:
        payload["variants"]["with_code"] = _run_variant(with_code=True)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"wrote {out} "
          f"({len(payload['variants'])} variant(s), {len(QUERIES)} queries)")
    return 0


def _table(rows: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for row in rows:
        out.append("")
        out.append(f"### {row['id']} — «{row['query']}»  ·  expected type: `{row['kind']}`")
        out.append("")
        out.append("| # | score | file | section / name | corpus |")
        out.append("|---|---|---|---|---|")
        for i, hit in enumerate(row["top"], 1):
            label = hit.get("section") or hit.get("name") or ""
            out.append(f"| {i} | {hit['score']:.4f} | `{hit['file']}` | "
                       f"{label} | {hit['source']} |")
        if not row["top"]:
            out.append("| — | — | *(no hits)* | | |")
        out.append("")
        out.append(f"*engine {row['engine_ms']} ms · wall {row['wall_ms']} ms*")
    return out


def _reject_a_foreign_query_set(data: dict, src: str) -> str | None:
    """Why this capture may not be read with the current QUERIES, or None.

    Rows are paired by `id`, so a capture whose Q01 asked a different question
    still lines up perfectly against today's Q01 and yields a displacement
    number about nothing. That became reachable on 2026-08-08, when the ten
    questions were re-worded into English while every recorded capture under
    `docs/` kept the ones it was taken with — the same shape
    `tests/test_search_eval.py` already refuses for the retrieval eval set
    ("the comparison is between different sets and means nothing").

    The test is EQUALITY of the two sets, not agreement on their overlap. The
    first version compared `recorded.keys() & current.keys()`, which refused the
    one shape it had just seen — same ids, re-worded text — and accepted a
    capture whose ids were entirely different, an empty one, and one with no
    `variants` key at all. The first rendered an empty table and exited 0; the
    last raised KeyError and exited 1, which in this repo's convention means
    "defects found" rather than "the tool did not run"."""
    variants = data.get("variants")
    if not isinstance(variants, dict) or not isinstance(variants.get("baseline"), list):
        return (f"{src} is not a capture: no `variants.baseline` list. "
                f"Take one with `capture`.")

    rows = variants["baseline"]
    if not all(isinstance(r, dict) and "id" in r for r in rows):
        return f"{src} has rows without an `id`; it cannot be paired at all."

    recorded = {r["id"]: r.get("query") for r in rows}
    current = {q["id"]: q["query"] for q in QUERIES}
    if recorded == current:
        return None

    missing = sorted(set(current) - set(recorded))
    extra = sorted(set(recorded) - set(current))
    drifted = sorted(i for i in recorded.keys() & current.keys()
                     if recorded[i] != current[i])

    detail = []
    if drifted:
        detail.append(f"  {len(drifted)} question(s) re-worded, first {drifted[0]}:\n"
                      f"    recorded: {recorded[drifted[0]]!r}\n"
                      f"    current:  {current[drifted[0]]!r}")
    if missing:
        detail.append(f"  absent from the capture: {', '.join(missing)}")
    if extra:
        detail.append(f"  in the capture but not in QUERIES: {', '.join(extra)}")

    return (f"{src} was captured with a different question set.\n"
            + "\n".join(detail) + "\n"
            "Pairing by id across two question sets measures nothing. Either take "
            "a fresh `capture`, or leave this file alone: a DATED record "
            "(a release baseline, a pre-corpus slice) documents a state that "
            "cannot be reproduced, and re-recording it destroys the evidence.")


def cmd_render(args: argparse.Namespace) -> int:
    data = json.loads(Path(args.src).read_text(encoding="utf-8"))
    refusal = _reject_a_foreign_query_set(data, args.src)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2
    lines = _table(data["variants"]["baseline"])
    print("\n".join(lines))
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    """Same-session diff: what `--with-code` does to the memory top-5.

    `displaced` is the number of baseline hits that fell out of the top-5,
    which is the exact quantity decision D2 was argued over."""
    data = json.loads(Path(args.src).read_text(encoding="utf-8"))
    refusal = _reject_a_foreign_query_set(data, args.src)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2
    base = {r["id"]: r for r in data["variants"]["baseline"]}
    withc = {r["id"]: r for r in data["variants"].get("with_code", [])}
    if not withc:
        print("capture was taken without --both; nothing to compare",
              file=sys.stderr)
        return 2
    print("| Q | code hits in top-5 | notes displaced | top-1 changed |")
    print("|---|---|---|---|")
    total_code = total_displaced = changed_top1 = 0
    for qid in sorted(base):
        b_ids = [h["chunk_id"] for h in base[qid]["top"]]
        w = withc[qid]["top"]
        w_ids = [h["chunk_id"] for h in w]
        code_hits = sum(1 for h in w if h["source"] == "code")
        displaced = sum(1 for c in b_ids if c not in w_ids)
        top1 = bool(b_ids and w_ids and b_ids[0] != w_ids[0])
        total_code += code_hits
        total_displaced += displaced
        changed_top1 += int(top1)
        print(f"| {qid} | {code_hits} | {displaced} | {'yes' if top1 else 'no'} |")
    n = len(base)
    print(f"| **total** | **{total_code}** / {n * TOP_K} | "
          f"**{total_displaced}** / {n * TOP_K} | **{changed_top1}** / {n} |")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("capture", help="run the control queries and store them")
    c.add_argument("--out", default="docs/code-index-baseline.json")
    c.add_argument("--both", action="store_true",
                   help="also run every query with --with-code (C06 evidence)")
    c.add_argument("--note", help="free-text provenance line stored in the JSON")
    c.set_defaults(func=cmd_capture)

    r = sub.add_parser("render", help="markdown tables for the baseline variant")
    r.add_argument("src", nargs="?", default="docs/code-index-baseline.json")
    r.set_defaults(func=cmd_render)

    d = sub.add_parser("compare", help="baseline vs --with-code displacement")
    d.add_argument("src", nargs="?", default="docs/code-index-baseline.json")
    d.set_defaults(func=cmd_compare)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
