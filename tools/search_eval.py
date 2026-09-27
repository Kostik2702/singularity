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
    python tools/search_eval.py chunker --scratch <empty dir> --out docs/search-eval-code-v2.json

`chunker` (CODE-GRAPH P7) never measures the live index: it copies the code
corpus's input and the memory index into `--scratch`, builds the code corpus
there with v1 and with v2, and gates v2 by the rule in `GATE`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from collections import defaultdict
from collections.abc import Callable
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


def _exists_as_spelled(root: Path, rel: str) -> bool:
    """True only if every segment of `rel` exists under `root` with exactly
    this spelling. `Path.exists()` is not enough: on a case-insensitive
    filesystem it says yes to `singularity/x.py` when the directory is
    `Singularity`, while ranking compares the spelling the index recorded --
    so the case would score a miss forever with verification green."""
    here = root
    for part in Path(rel).parts:
        try:
            if part not in os.listdir(here):
                return False
        except OSError:
            return False
        here = here / part
    return True


def verify_targets(cases: list[dict[str, Any]], vault: Path) -> list[str]:
    """Every expected note must exist, spelled as on disk. Returns the missing ones."""
    missing = []
    for case in cases:
        for rel in case["expect"]:
            if not _exists_as_spelled(vault, rel):
                missing.append(f"{case['id']} -> {rel}")
    return missing


def run_once(cases: list[dict[str, Any]], hybrid: bool | None,
             corpus: str = "memory",
             search: Callable[[dict[str, Any]], dict[str, Any]] | None = None
             ) -> dict[str, Any]:
    """`search`: answers one payload. None = the live `mb search` path (daemon,
    cold fallback) against this machine's config -- what the tool always did.
    The chunker gate passes an in-process engine over an isolated copy."""
    if search is None:
        from singularity.config import load_config
        from singularity.search import client

        cfg = load_config()

        def search(payload: dict[str, Any]) -> dict[str, Any]:
            return client.search(cfg, payload)

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
        resp = search(payload)
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


# --- chunker v2 against v1, on an isolated copy (CODE-GRAPH P7) -------------
#
# The rule below was written BEFORE the first run and is not tuned after it.
# Both fusion modes are gated: fusion on is what `mb search --with-code`
# serves for code (the engine fuses code whatever the config says), fusion
# off is the pure vector ranking, where chunk boundaries act most directly.

GATE_METRICS = ("recall@1", "recall@5", "recall@10", "mrr")
GATE = ("v2 is not worse than v1 when, in both fusion modes (on and off), every _overall "
        "metric -- recall@1, recall@5, recall@10 and MRR -- of v2 is at least v1's minus "
        "the weight of ONE case (1/n). Per-class numbers are reported, not gated: with 6-8 "
        "cases a class, one case moves a class metric by 12-17 points.")
# `.claude/worktrees` inside a repository holds whole copies of it that
# parallel agents are editing: indexed, every target would get several
# near-identical twins, and they would change between the two builds.
SCRATCH_EXTRA_SKIP = ("worktrees",)
# config values that shape a code search; everything else stays at its default
_CARRIED = ("model", "min_score", "excerpt_chars", "hybrid_search", "batch_size",
            "encoder_device", "code_vendored_pattern")


def gate_verdict(v1: dict[str, Any], v2: dict[str, Any]) -> dict[str, Any]:
    """v1, v2: {"fusion_on": run, "fusion_off": run}. Pure, so the suite can
    prove the rule on synthetic numbers."""
    checks = []
    for mode in ("fusion_on", "fusion_off"):
        a, b = v1[mode]["metrics"]["_overall"], v2[mode]["metrics"]["_overall"]
        one_case = 1.0 / a["n"]
        for metric in GATE_METRICS:
            floor = round(a[metric] - one_case, 4)
            checks.append({"mode": mode, "metric": metric, "v1": a[metric], "v2": b[metric],
                           "floor": floor, "pass": b[metric] >= floor - 1e-9})
    return {"rule": GATE, "not_worse": all(c["pass"] for c in checks), "checks": checks}


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _tree_digest(directory: Path) -> str | None:
    if not directory.is_dir():
        return None
    rows = [f"{p.relative_to(directory).as_posix()}\t{_sha256(p)}"
            for p in sorted(directory.rglob("*")) if p.is_file()]
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def _refuse_inside(scratch: Path, *real: Path | None) -> None:
    """The copy exists so that nothing can write where a live index or a live
    repository lives; a scratch directory inside one of them defeats it."""
    target = scratch.resolve()
    for parent in real:
        if parent is None:
            continue
        try:
            target.relative_to(parent.resolve())
        except ValueError:
            continue
        raise SystemExit(f"[eval] refusing scratch {scratch}: it is inside {parent}")


def _isolated_data(real: Any, scratch: Path, **extra: Any) -> dict[str, Any]:
    data = {k: real.get(k) for k in _CARRIED if real.get(k) is not None}
    data.update(code_repos_dir=str(scratch / "repos"), hf_cache=str(real.hf_cache),
                code_skip_dirs=[*(real.get("code_skip_dirs") or []), *SCRATCH_EXTRA_SKIP],
                code_graph_extra_roots=[], ingest_index_sources=False,
                # read only by `mb code` and `--graph-boost`; indexing ignores it
                code_graph_enabled=True)
    data.update(extra)
    return data


def _copy_memory_index(real_dir: Path, vault: Path) -> dict[str, Any]:
    """The live memory corpus's committed generation, copied byte for byte:
    the control queries rank notes against code, and the builds below must
    be seen to leave this copy alone (D3)."""
    from singularity.search import indexer

    files = indexer.index_files(real_dir)
    if files is None:
        return {"copied": False}
    gen = files[0].parent
    name = gen.name if gen.name.startswith("gen_") else "gen_copy"
    target = vault / ".mb" / "memory_index"
    (target / name).mkdir(parents=True, exist_ok=True)
    for fname in ("embeddings.npy", "metadata.json", indexer.SUMMARY_FILE):
        if (gen / fname).is_file():
            shutil.copy2(gen / fname, target / name / fname)
    (target / indexer.CURRENT_FILE).write_text(name, encoding="utf-8")
    return {"copied": True, "generation": name, "digest": _tree_digest(target)}


def prepare_isolated(scratch: Path, schemas: tuple[str, ...] = ("v1", "v2")) -> dict[str, Any]:
    """Freeze the code corpus's input, copy the memory corpus, build the code
    corpus once per schema and a code graph -- all under `scratch`.

    What touches the machine: READS of the repositories root, the live memory
    index and the model cache. Nothing is written outside `scratch`; the live
    CURRENT pointers are hashed before and after to show it."""
    from singularity.codegraph import build, scope
    from singularity.config import Config, load_config
    from singularity.search import indexer

    real = load_config()
    if real.code_repos_dir is None:
        raise SystemExit("[eval] code_repos_dir is not set on this machine")
    _refuse_inside(scratch, real.vault, real.code_repos_dir)
    if scratch.exists() and any(scratch.iterdir()):
        raise SystemExit(f"[eval] {scratch} is not empty; give a fresh directory or --reuse")
    live = {"memory_CURRENT": real.memory_index_dir / indexer.CURRENT_FILE,
            "code_CURRENT": real.code_index_dir / indexer.CURRENT_FILE}
    before = {k: _sha256(p) for k, p in live.items()}

    # 1. the input, frozen: the two builds must read the same bytes
    scan = Config(vault=scratch / "scan",
                  data=_isolated_data(real, scratch, code_repos_dir=str(real.code_repos_dir)))
    report: dict[str, Any] = {}
    selected = indexer._scan_corpus(indexer.code_corpus(scan), report)
    rows = []
    for rel, src in sorted(selected.items()):
        dst = scratch / "repos" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        rows.append(f"{rel}\t{_sha256(dst)}")
    snapshot = {"source": str(real.code_repos_dir), "files": len(rows),
                "digest": hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest(),
                "extra_skip": list(SCRATCH_EXTRA_SKIP),
                "excluded": [e["path"] for e in report.get("excluded") or []],
                "live_index_files": (indexer.index_summary(real.code_index_dir) or {})
                .get("file_count")}

    configs: dict[str, dict[str, Any]] = {}
    memory: dict[str, Any] = {}
    builds: dict[str, Any] = {}
    for schema in schemas:
        name = f"vault_{schema}"
        configs[schema] = {"vault": str(scratch / name),
                           "data": _isolated_data(real, scratch, code_chunk_schema=schema)}
        memory[schema] = {"before": _copy_memory_index(real.memory_index_dir, scratch / name)}
    # 2. the code corpus, once per schema, each into its own vault
    for schema in schemas:
        cfg = Config(vault=Path(configs[schema]["vault"]), data=configs[schema]["data"])
        started = time.perf_counter()
        stats = indexer.rebuild(cfg, progress=False, corpus="code")
        _, meta_path = indexer.index_files(cfg.code_index_dir)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        builds[schema] = {"seconds": round(time.perf_counter() - started, 1), **stats,
                          "chunk_schema": meta.get("chunk_schema"),
                          "grammar_pins": meta.get(indexer.GRAMMAR_PINS_KEY),
                          "file_meta_digest": hashlib.sha256(json.dumps(
                              meta["file_meta"], sort_keys=True).encode("utf-8")).hexdigest()}
        memory[schema]["after"] = _tree_digest(cfg.memory_index_dir)
    # 3. the graph the boost reads: built once, copied next to every schema,
    #    so `--graph-boost` finds it where the real CLI looks (`code_graph_dir`)
    first = Config(vault=Path(configs[schemas[0]]["vault"]), data=configs[schemas[0]]["data"])
    started = time.perf_counter()
    units = scope.discover_units(first)
    for unit in units:
        build.build_unit(first, unit)
    for schema in schemas[1:]:
        other = Config(vault=Path(configs[schema]["vault"]), data=configs[schema]["data"])
        shutil.copytree(first.code_graph_dir, other.code_graph_dir)
    graph = {"units": len(units), "seconds": round(time.perf_counter() - started, 1),
             "digest": _tree_digest(first.code_graph_dir)}

    after = {k: _sha256(p) for k, p in live.items()}
    manifest = {"schemas": list(schemas), "configs": configs, "snapshot": snapshot,
                "builds": builds, "graph": graph, "memory_copy": memory,
                "live": {"vault": str(real.vault), "before": before, "after": after,
                         "untouched": before == after}}
    (scratch / "setup.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    return manifest


def load_isolated(scratch: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """(manifest, {schema: Config}) of a prepared copy."""
    from singularity.config import Config

    manifest = json.loads((scratch / "setup.json").read_text(encoding="utf-8"))
    cfgs = {name: Config(vault=Path(c["vault"]), data=c["data"])
            for name, c in manifest["configs"].items()}
    return manifest, cfgs


def cmd_chunker(args: argparse.Namespace) -> int:
    from singularity.search import graph_boost
    from singularity.search.engine import SearchEngine

    scratch = Path(args.scratch)
    if not args.reuse:
        prepare_isolated(scratch)
    manifest, cfgs = load_isolated(scratch)
    cases = load_fixture()["code_cases"]
    missing = verify_targets(cases, scratch / "repos")
    if missing:
        print(f"[eval] {len(missing)} expected file(s) missing from the copy:", file=sys.stderr)
        for m in missing:
            print(f"   {m}", file=sys.stderr)
        return 2

    runs: dict[str, dict[str, Any]] = {}
    for schema in manifest["schemas"]:
        engine = SearchEngine(cfgs[schema])

        def boosted(payload: dict[str, Any], engine: SearchEngine = engine,
                    cfg: Any = cfgs[schema]) -> dict[str, Any]:
            return graph_boost.apply(cfg, engine.search(payload))

        runs[schema] = {"fusion_off": run_once(cases, False, "code", engine.search),
                        "fusion_on": run_once(cases, True, "code", engine.search),
                        "fusion_on_boost": run_once(cases, True, "code", boosted)}
        for label, run in runs[schema].items():
            print(f"-- schema {schema}, {label}")
            render(run, show_misses=not args.quiet)
            print()
    verdict = gate_verdict(runs["v1"], runs["v2"])
    for check in verdict["checks"]:
        print(f"   {check['mode']:<11}{check['metric']:<10} v1 {check['v1']:.4f}  "
              f"v2 {check['v2']:.4f}  floor {check['floor']:.4f}  "
              f"{'ok' if check['pass'] else 'WORSE'}")
    print(f"\n[eval] chunker v2 {'is not worse than' if verdict['not_worse'] else 'is WORSE than'} v1")

    # D5, layer 3, on both builds: v2 indexes module headers (constants,
    # imports) that v1 never emitted, so its fact-check must be re-run on it.
    # Counts only -- samples of a match are exactly what must not be committed.
    from tools.code_index_audit import audit

    privacy = {}
    for schema in manifest["schemas"]:
        rep = audit(cfgs[schema])
        privacy[schema] = {k: rep.get(k) for k in ("chunks", "files", "fallback_share",
                                                   "secret_marker_total", "by_kind")}
        privacy[schema]["markers"] = {name: f["count"]
                                      for name, f in (rep.get("secret_markers") or {}).items()}
        print(f"[audit] {schema}: {privacy[schema]['secret_marker_total']} secret marker(s) "
              f"{privacy[schema]['markers']}")

    p0 = REPO / "docs" / "search-eval-code-baseline.json"
    p0_runs = json.loads(p0.read_text(encoding="utf-8")).get("runs", []) if p0.is_file() else []
    payload = {
        "about": "Chunker v2 (definition boundaries) against v1 (regex) on the code eval set, "
                 "same session, same frozen input, same model; plus the graph boost on the "
                 "served fusion mode. CODE-GRAPH P7.",
        "taken_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "gate": verdict,
        "isolation": {k: manifest[k] for k in ("snapshot", "live", "memory_copy", "graph")},
        "builds": manifest["builds"],
        "privacy_audit": privacy,
        "runs": runs,
        "p0_record": {("fusion_on" if r.get("hybrid") else "fusion_off"): r["metrics"]
                      for r in p0_runs},
    }
    if args.out:
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                                  encoding="utf-8")
        print(f"[eval] wrote {args.out}")
    return 0 if verdict["not_worse"] else 1


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

    k = sub.add_parser("chunker", help="chunker v2 vs v1 (and the graph boost) on the code "
                                       "set, built into an isolated copy")
    k.add_argument("--scratch", required=True,
                   help="an EMPTY directory outside the vault and the repositories root")
    k.add_argument("--reuse", action="store_true", help="measure a copy prepared earlier")
    k.add_argument("--out", help="write the record as JSON (docs/search-eval-code-v2.json)")
    k.add_argument("--quiet", action="store_true", help="metrics only, no miss list")

    args = ap.parse_args(argv)
    if args.cmd == "chunker":
        return cmd_chunker(args)

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
