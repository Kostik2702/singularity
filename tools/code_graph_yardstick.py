"""Ground-truth yardstick for the code graph's CALL resolution (CODE-GRAPH P0).

Why this exists before any graph code does: the council found that the only
published precision figure for this kind of tool was measured on a different
resolver from the one whose speed was claimed, and that a name-guessing
resolver scores 52% on a real Python monorepo while its README said ~95%.
A precision claim is an opinion until it is scored against call sites a person
verified by reading both ends. This tool holds that set and scores against it.

The set lives in `tests/fixtures/code_graph_edges.json`: real call sites in
real repositories under `code_repos_dir`, each labelled with the qname it
really calls -- or `null` when the true answer is "no first-party edge"
(stdlib, a library, a builtin that merely SHARES a name with our code).

A yardstick that cannot fail measures nothing. `baseline` runs two naive
resolvers that look only at the called NAME, and P0 is accepted only if they
score below the owner's thresholds on this set. If a name-only guess passes,
the set is too easy and must be sharpened before it gates anything.

A labelled site that moved is NOT silently skipped: the file hash is pinned,
and when it changes the call text is looked up again. Intact -> scored with a
note; relocated -> scored at the new line; gone -> exit 2, because a set that
quietly shrinks under edits reports a rising score for the wrong reason.

Usage:
    python tools/code_graph_yardstick.py check            # are the labels still true?
    python tools/code_graph_yardstick.py baseline         # score the name-only resolvers
    python tools/code_graph_yardstick.py score preds.json # score a resolver's answers
       preds.json = {"<edge id>": "<qname>" | null, ...}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

FIXTURE = REPO / "tests" / "fixtures" / "code_graph_edges.json"
RELOCATE_WINDOW = 40   # lines searched either side when a pinned file changed

# The P1 acceptance gate, set by the owner on 2026-09-26 against the P0 set
# (decision CODE-GRAPH D1). Both naive resolvers fail it on that set: name-first
# 0.56 precision / 0.83 false edges, name-unique 0.74 / 0.50. Recall is set
# below precision on purpose: an abstention is recoverable (the answer carries
# `unresolved_candidates`), a wrong edge in impact analysis is not.
GATE = {"precision_min": 0.90, "recall_min": 0.65, "false_edge_max": 0.10}


def gate(overall: dict[str, Any]) -> list[str]:
    """The ways a resolver's overall score misses the owner's gate, as sentences."""
    misses = []
    if overall["precision"] is None or overall["precision"] < GATE["precision_min"]:
        misses.append(f"precision {overall['precision']} < {GATE['precision_min']}")
    if overall["recall"] is None or overall["recall"] < GATE["recall_min"]:
        misses.append(f"recall {overall['recall']} < {GATE['recall_min']}")
    if overall["false_edge_rate"] is not None and overall["false_edge_rate"] > GATE["false_edge_max"]:
        misses.append(f"false-edge rate {overall['false_edge_rate']} > {GATE['false_edge_max']}")
    return misses

CATEGORIES = (
    "same-file", "import-resolved", "self-method", "this-method", "inherited",
    "module-level", "common-name", "external-collision", "builtin-external",
    "needs-types", "decorated", "barrel", "di-member", "test-calls-prod",
)


# --- scoring: pure, so the suite gates it without any repository ------------

def score(edges: list[dict[str, Any]], predictions: dict[str, str | None]) -> dict[str, Any]:
    """Precision, recall and false-edge rate, overall and per category.

    Per labelled site with truth E and prediction P:
      E is a qname:  P == E -> tp;  P is None -> miss;  P != E -> wrong (fp + miss)
      E is None:     P is None -> tn;  P is a qname -> false edge (fp)
    A site absent from `predictions` counts as P = None: an unanswered site
    is a miss for a first-party call and a correct abstention for an
    external one, which is exactly how a resolver that skips it behaves.
    """
    buckets: dict[str, dict[str, int]] = defaultdict(
        lambda: {"n": 0, "tp": 0, "wrong": 0, "miss": 0, "tn": 0, "false_edge": 0})
    for e in edges:
        truth = e["expect"]
        pred = predictions.get(e["id"])
        for key in ("_overall", e["category"]):
            b = buckets[key]
            b["n"] += 1
            if truth is None:
                if pred is None:
                    b["tn"] += 1
                else:
                    b["false_edge"] += 1
            elif pred == truth:
                b["tp"] += 1
            elif pred is None:
                b["miss"] += 1
            else:
                b["wrong"] += 1
    out: dict[str, Any] = {}
    for key, b in buckets.items():
        emitted = b["tp"] + b["wrong"] + b["false_edge"]
        first_party = b["tp"] + b["wrong"] + b["miss"]
        external = b["tn"] + b["false_edge"]
        out[key] = dict(b,
                        precision=round(b["tp"] / emitted, 4) if emitted else None,
                        recall=round(b["tp"] / first_party, 4) if first_party else None,
                        false_edge_rate=round(b["false_edge"] / external, 4) if external else None)
    return out


# --- the fixture -------------------------------------------------------------

def load_fixture(path: Path = FIXTURE) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_fixture(data: dict[str, Any]) -> list[str]:
    """Structural problems, as sentences. Empty list = well formed."""
    problems: list[str] = []
    ids = [e.get("id") for e in data.get("edges", [])]
    if len(ids) != len(set(ids)):
        problems.append("duplicate edge ids")
    units = data.get("units", {})
    for e in data.get("edges", []):
        eid = e.get("id")
        for field in ("unit", "file", "line", "call_text", "caller",
                      "callee_name", "category", "why"):
            if field not in e or e[field] in ("", None):
                problems.append(f"{eid}: missing {field}")
        if "expect" not in e:
            problems.append(f"{eid}: missing expect (use null for no first-party edge)")
        elif e["expect"] is not None and "::" not in e["expect"]:
            problems.append(f"{eid}: expect is not a qname: {e['expect']!r}")
        if e.get("category") not in CATEGORIES:
            problems.append(f"{eid}: unknown category {e.get('category')!r}")
        if e.get("unit") not in units:
            problems.append(f"{eid}: unit {e.get('unit')!r} not declared")
        key = f"{e.get('unit')}|{e.get('file')}"
        if key not in data.get("files", {}):
            problems.append(f"{eid}: no pinned sha256 for {key}")
        if Path(str(e.get("file", ""))).is_absolute() or ".." in Path(str(e.get("file", ""))).parts:
            problems.append(f"{eid}: file must be unit-relative")
    return problems


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def locate(edges: list[dict[str, Any]], files: dict[str, str],
           unit_root: Callable[[str], Path]) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Re-find every labelled site on disk.

    Returns (live edges with the CURRENT line, notes, failures). A failure is a
    label that can no longer be trusted: its file is gone, or its call text is
    no longer anywhere near where it was.
    """
    live: list[dict[str, Any]] = []
    notes: list[str] = []
    failures: list[str] = []
    text_cache: dict[Path, list[str]] = {}
    for e in edges:
        path = unit_root(e["unit"]) / e["file"]
        if not path.is_file():
            failures.append(f"{e['id']}: file gone: {e['unit']}/{e['file']}")
            continue
        if path not in text_cache:
            text_cache[path] = path.read_text(encoding="utf-8", errors="replace").splitlines()
        lines = text_cache[path]
        want = e["call_text"].strip()
        pinned = files.get(f"{e['unit']}|{e['file']}")
        line = int(e["line"])
        if 1 <= line <= len(lines) and want in lines[line - 1]:
            if pinned and pinned != _sha256(path):
                notes.append(f"{e['id']}: file changed, call site intact at line {line}")
            live.append(e)
            continue
        lo, hi = max(1, line - RELOCATE_WINDOW), min(len(lines), line + RELOCATE_WINDOW)
        found = [n for n in range(lo, hi + 1) if want in lines[n - 1]]
        if len(found) == 1:
            notes.append(f"{e['id']}: relocated {line} -> {found[0]}")
            live.append(dict(e, line=found[0]))
        else:
            why = "not found" if not found else f"ambiguous ({len(found)} matches)"
            failures.append(f"{e['id']}: call text {why} near line {line} in {e['unit']}/{e['file']}")
    return live, notes, failures


# --- the naive resolvers the set must be able to fail ------------------------

_PY_DEF = re.compile(r"^([ \t]*)(?:async\s+)?(def|class)\s+([A-Za-z_]\w*)")
_TS_CLASS = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)")
_TS_FUNC = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)")
_TS_CONST_FN = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=>")
_TS_METHOD = re.compile(
    r"^\s+(?:(?:public|private|protected|static|readonly|override|abstract|async|get|set)\s+)*"
    r"([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\([^;]*$")
_TS_NOT_METHODS = {"if", "for", "while", "switch", "catch", "return", "function",
                   "constructor", "super", "new", "await", "typeof", "else"}
_PY_SUFFIXES = (".py",)
_TS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_SKIP_PARTS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build"}


def _py_defs(rel: str, lines: list[str]) -> list[tuple[str, str]]:
    stack: list[tuple[int, str]] = []
    out = []
    for raw in lines:
        m = _PY_DEF.match(raw)
        if not m:
            continue
        indent = len(m.group(1).expandtabs(4))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        name = m.group(3)
        qname = ".".join([s[1] for s in stack] + [name])
        out.append((name, f"{rel}::{qname}"))
        stack.append((indent, name))
    return out


def _ts_defs(rel: str, lines: list[str]) -> list[tuple[str, str]]:
    out = []
    cls: str | None = None
    depth = 0
    cls_depth = -1
    for raw in lines:
        m = _TS_CLASS.match(raw)
        if m:
            cls, cls_depth = m.group(1), depth
            out.append((cls, f"{rel}::{cls}"))
        else:
            m = _TS_FUNC.match(raw) or _TS_CONST_FN.match(raw)
            if m and (cls is None or depth <= cls_depth):
                out.append((m.group(1), f"{rel}::{m.group(1)}"))
            elif cls is not None and depth == cls_depth + 1:
                mm = _TS_METHOD.match(raw)
                if mm and mm.group(1) not in _TS_NOT_METHODS:
                    out.append((mm.group(1), f"{rel}::{cls}.{mm.group(1)}"))
        depth += raw.count("{") - raw.count("}")
        if cls is not None and depth <= cls_depth:
            cls, cls_depth = None, -1
    return out


def definitions_by_name(root: Path) -> dict[str, list[str]]:
    """Every def/class/function name in a unit -> the qnames that carry it,
    sorted by path. Regex, not a parser: this is the NAIVE baseline."""
    table: dict[str, list[str]] = defaultdict(list)
    for path in sorted(root.rglob("*")):
        if not path.is_file() or _SKIP_PARTS.intersection(path.relative_to(root).parts):
            continue
        suffix = path.suffix.lower()
        if suffix not in _PY_SUFFIXES + _TS_SUFFIXES:
            continue
        rel = path.relative_to(root).as_posix()
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        pairs = _py_defs(rel, lines) if suffix in _PY_SUFFIXES else _ts_defs(rel, lines)
        for name, qname in pairs:
            table[name].append(qname)
    return table


def name_first(edges: list[dict[str, Any]], tables: dict[str, dict[str, list[str]]]) -> dict[str, str | None]:
    """Guess: the first definition anywhere in the unit that carries the name."""
    return {e["id"]: (tables[e["unit"]].get(e["callee_name"]) or [None])[0] for e in edges}


def name_unique(edges: list[dict[str, Any]], tables: dict[str, dict[str, list[str]]]) -> dict[str, str | None]:
    """Guess only when exactly one definition carries the name, else abstain."""
    out: dict[str, str | None] = {}
    for e in edges:
        hits = tables[e["unit"]].get(e["callee_name"]) or []
        out[e["id"]] = hits[0] if len(hits) == 1 else None
    return out


# --- CLI ---------------------------------------------------------------------

def _unit_root_factory() -> Callable[[str], Path]:
    from singularity.config import load_config
    cfg = load_config()
    if cfg.code_repos_dir is None:
        raise SystemExit("[yardstick] code_repos_dir is not set; the labelled units live under it")
    base = Path(cfg.code_repos_dir)
    return lambda unit: base / unit


def _render(name: str, scores: dict[str, Any]) -> None:
    print(f"== {name} ==")
    print(f"{'category':<20}{'n':>4}{'prec':>8}{'recall':>8}{'false-edge':>12}")
    order = ["_overall"] + sorted(k for k in scores if k != "_overall")
    for key in order:
        s = scores[key]
        fmt = lambda v: "   -" if v is None else f"{v:.2f}"   # noqa: E731
        print(f"{key:<20}{s['n']:>4}{fmt(s['precision']):>8}{fmt(s['recall']):>8}"
              f"{fmt(s['false_edge_rate']):>12}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="are the labelled sites still where they were?")
    b = sub.add_parser("baseline", help="score the name-only resolvers the set must defeat")
    b.add_argument("--out", help="write the scores as JSON")
    s = sub.add_parser("score", help="score a resolver's predictions")
    s.add_argument("predictions")
    args = ap.parse_args(argv)

    data = load_fixture()
    problems = validate_fixture(data)
    if problems:
        print("[yardstick] the fixture is malformed:", file=sys.stderr)
        for p in problems:
            print(f"   {p}", file=sys.stderr)
        return 2

    unit_root = _unit_root_factory()
    live, notes, failures = locate(data["edges"], data["files"], unit_root)
    for n in notes:
        print(f"[yardstick] {n}")
    if failures:
        print(f"[yardstick] {len(failures)} label(s) can no longer be trusted -- "
              f"re-verify them; the set measures nothing until then:", file=sys.stderr)
        for f in failures:
            print(f"   {f}", file=sys.stderr)
        return 2
    if args.cmd == "check":
        print(f"[yardstick] {len(live)} labelled sites verified on disk")
        return 0

    if args.cmd == "score":
        preds = json.loads(Path(args.predictions).read_text(encoding="utf-8"))
        sc = score(live, preds)
        _render("resolver", sc)
        misses = gate(sc["_overall"])
        for m in misses:
            print(f"[yardstick] GATE MISSED: {m}", file=sys.stderr)
        return 1 if misses else 0

    tables = {unit: definitions_by_name(unit_root(unit)) for unit in data["units"]}
    runs = {"name-first": score(live, name_first(live, tables)),
            "name-unique": score(live, name_unique(live, tables))}
    for name, sc in runs.items():
        _render(name, sc)
        misses = gate(sc["_overall"])
        print(f"gate: {'PASSED -- the set cannot fail this naive resolver, sharpen it' if not misses else 'missed (' + '; '.join(misses) + ')'}")
        print()
    if args.out:
        Path(args.out).write_text(json.dumps(runs, indent=1), encoding="utf-8")
        print(f"[yardstick] wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
