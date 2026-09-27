"""Chunker v2 for the code corpus: definition boundaries from the parser
(CODE-GRAPH D1, row "Chunker v2"; plan P7.2).

v1 (`code_chunking`) finds symbols with regular expressions and stays the
default. v2 asks the SAME extractor the code graph uses (`codegraph.extract_py`
/ `extract_ts`) where every definition starts and ends, and cuts there. The
graph's store (`graph.db`) is never read: the library is shared, the data is
not, so a chunk can never depend on whether a graph was built.

What v2 changes, and why each change:

* Boundaries come from the parse tree. A class, its methods, a TypeScript
  class member or an arrow function assigned to an exported const each start
  and end where the grammar says -- v1 only knew where the NEXT regex match
  began, so trailing class attributes and comments were glued to whichever
  method preceded them.
* One chunk per unit, where a unit is a definition at module level or directly
  in a class (recursively). A closure or a class declared inside a function
  stays inside that function's chunk: it is part of the function's logic, and
  cutting it out would leave the function in two halves around a hole.
  Interface and type members stay inside their interface: they are its text,
  not code of their own.
* No text is lost. v1 cut a symbol's body at `MAX_BODY_LINES` and dropped
  every line above the first symbol (imports, module docstring, constants).
  v2 windows a long unit instead of truncating it and chunks the module-level
  remainder the way v1 chunks a file without symbols -- the same invariant the
  markdown chunker keeps since AUD-03 ("every non-empty window is kept,
  including a small trailing one"). A fragment below `MIN_CHUNK_CHARS` is not
  dropped either: it is merged into its neighbour, which is contiguous with it.
  The only text v2 does not emit is a file whose ENTIRE text is below the
  minimum, as in v1.
* Chunk metadata carries `qname` (`<file>::<Outer>.<inner>`, the graph's
  identity format with a corpus-relative path) and the embedded head names
  the unit by its dotted local name, so `SessionStore.size` is findable as
  such. Everything else -- description, repo, text shape, windows of the
  remainder -- is v1's own helpers, so an evaluation of v2 against v1 measures
  the boundaries and not a different description format.

The schema is chosen by config (`code_chunk_schema`), never by whether the
parser happens to be installed (D1, UNMET r). Which grammar versions a v2
corpus may be written with is decided by the PINS in the lock, not by what
the interpreter loaded -- see `writer_guard`.
"""
from __future__ import annotations

import importlib
import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from ..codegraph.versions import PARSER_PACKAGES
from ..log import log
from . import code_chunking as v1

# Distinct from v1's CODE_CHUNK_SCHEMA_VERSION on purpose: switching the
# config either way is a chunk-format change of the code corpus, and D3's
# per-corpus comparison in `indexer._update` turns it into a rebuild of that
# corpus alone.
CODE_CHUNK_SCHEMA_VERSION = 2

# The lock that pins the parser: exact versions with wheel hashes, the file
# `pip install --require-hashes` installs from. It is the ONE pin source:
#   * the pyproject `graph` extra reaches a running engine only through the
#     installed metadata, which an editable install freezes at the last
#     `pip install -e` (on the machine this was written on, the installed
#     metadata had no `graph` extra at all, while the pyproject did);
#   * pyproject.toml needs `tomllib` (3.11+) and the engine runs on 3.10;
#   * the engine is always installed editable (install/install.py), so the
#     lock sits next to the package it pins.
# tests/test_code_chunk_schema.py keeps the pyproject copy equal to this one.
PINS_FILE = Path(__file__).resolve().parents[2] / "requirements-graph.txt"

_PIN_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([A-Za-z0-9][A-Za-z0-9._+!-]*)")


class GrammarPinRefused(RuntimeError):
    """A writer refused to touch the code corpus because of its parser.

    Raised BEFORE anything is scanned or written, so the code corpus's CURRENT
    and every other corpus stay exactly as they were. Never answered by
    falling back to v1: a v1 corpus written under a v2 config would be a
    schema chosen by the state of the interpreter, which D1 forbids."""


def _canonical(name: str) -> str:
    """PEP 503 name normalisation: `tree_sitter` and `Tree-Sitter` are one package."""
    return re.sub(r"[-_.]+", "-", name).lower()


def pinned_grammar_versions(path: Path | None = None) -> dict[str, str]:
    """{parser package: pinned version} from the lock, for every package the
    code graph calls its parser (`codegraph.versions.PARSER_PACKAGES`)."""
    source = path or PINS_FILE
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise GrammarPinRefused(
            f"code_chunk_schema v2: the grammar pins cannot be read ({source}: {exc}). "
            f"The code index is untouched.") from exc
    found: dict[str, str] = {}
    for line in text.splitlines():
        m = _PIN_RE.match(line.split("#", 1)[0])
        if m:
            found[_canonical(m.group(1))] = m.group(2)
    missing = [p for p in PARSER_PACKAGES if _canonical(p) not in found]
    if missing:
        raise GrammarPinRefused(
            f"code_chunk_schema v2: {source.name} pins no exact version for "
            f"{', '.join(missing)}; an unpinned grammar is a corpus whose boundaries move "
            f"with whatever wheel is installed. The code index is untouched.")
    return {p: found[_canonical(p)] for p in PARSER_PACKAGES}


def loaded_grammar_versions() -> dict[str, str]:
    """What THIS interpreter would import, by installed distribution metadata;
    `absent` for a package that is not installed."""
    out: dict[str, str] = {}
    for pkg in PARSER_PACKAGES:
        try:
            out[pkg] = version(pkg)
        except PackageNotFoundError:
            out[pkg] = "absent"
    return out


def load_extractors() -> dict[str, Any]:
    """The shared extractor modules, keyed by the v1 language label.

    Importing them builds every grammar's `Language`, which is exactly where
    a grammar that cannot load fails (a missing wheel, an ABI the core does
    not accept, a DLL that will not load) -- so this IS the load check."""
    try:
        py = importlib.import_module("singularity.codegraph.extract_py")
        ts = importlib.import_module("singularity.codegraph.extract_ts")
    except Exception as exc:  # noqa: BLE001 -- any failure to load a grammar is the same refusal
        raise GrammarPinRefused(
            f"code_chunk_schema v2: a grammar cannot load ({type(exc).__name__}: {exc}). "
            f"Refusing to write the code index rather than chunking it with v1 -- the "
            f"index is untouched. Install the pinned parser: "
            f"python -m pip install --require-hashes -r requirements-graph.txt") from exc
    return {"python": py, "ts": ts}


def writer_guard() -> dict[str, str]:
    """Called by the indexer before a v2 write of the code corpus. Returns the
    pins to stamp on the generation; raises `GrammarPinRefused` otherwise.

    The stamp is the PIN, not what was loaded (D1). Stamping the loaded
    versions would make two interpreters with different wheels rebuild the
    corpus every time they took turns; stamping the pin makes the one whose
    wheels differ from the lock refuse, and the one that matches carry on --
    0 rebuilds, 1 refusal. A rebuild happens only when the lock itself moves
    (`indexer._update` compares the stamp with the current pins)."""
    try:
        pins = pinned_grammar_versions()
        running = loaded_grammar_versions()
        differ = {p: (running.get(p, "absent"), v) for p, v in pins.items()
                  if running.get(p) != v}
        if differ:
            named = ", ".join(f"{p} {have} (pinned {want})" for p, (have, want) in differ.items())
            raise GrammarPinRefused(
                f"code_chunk_schema v2: this interpreter's parser differs from the pins: "
                f"{named}. Refusing to write the code index with other grammars than the "
                f"ones it is pinned to -- the index is untouched. Install the pinned wheels: "
                f"python -m pip install --require-hashes -r requirements-graph.txt")
        load_extractors()
    except GrammarPinRefused as exc:
        log.warning("[mb] WARNING: %s", exc)
        raise
    return pins


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------

def _split_lines(text: str) -> list[str]:
    """Lines as the parser counts them: by `\\n` only. `str.splitlines` also
    breaks on form feeds, `\\x1c`-`\\x1e` and U+2028, which would shift every
    line number after one of them away from the parser's rows (and from what
    an editor shows)."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [ln[:-1] if ln.endswith("\r") else ln for ln in lines]


def _depth(qname: str, by_q: dict[str, Any]) -> int:
    depth, parent = 0, by_q[qname].parent
    while parent is not None and parent in by_q:
        depth += 1
        parent = by_q[parent].parent
    return depth


def _span_text(lines: list[str], spans: list[tuple[int, int]]) -> str:
    return "\n".join("\n".join(lines[a - 1:b]) for a, b in spans)


def _units(definitions: list[Any], lines: list[str]) -> list[Any]:
    """Definitions that get chunks of their own, outermost first.

    A unit sits at module level or directly in a class that is itself a unit.
    Anything smaller than `MIN_CHUNK_CHARS` stays in the region around it --
    v1 dropped such a stub, v2 keeps its text where it stands."""
    by_q = {d.qname: d for d in definitions}
    chosen: set[str] = set()
    ordered = sorted(definitions, key=lambda d: (_depth(d.qname, by_q), d.spans[0]))
    for d in ordered:
        if d.parent is not None:
            parent = by_q.get(d.parent)
            if parent is None or d.parent not in chosen or parent.kind != "class":
                continue
        if len(_span_text(lines, d.spans).strip()) < v1.MIN_CHUNK_CHARS:
            continue
        chosen.add(d.qname)
    return [d for d in ordered if d.qname in chosen]


def _decorated_start(lines: list[str], start: int, decorators: list[str]) -> int:
    """First line of the decorators above `start` (0-based).

    Contiguous one-line `@decorator` lines are v1's rule. A decorator that
    spans lines (`@app.route(\\n "/x",\\n)`, `@pytest.mark.parametrize(...)`)
    has continuation lines that do not start with `@`; the extractor reports
    each decorator's text, so the number of lines they occupy is known and the
    walk up can include exactly those."""
    top = v1._extend_over_decorators(lines, start)
    if not any("\n" in d for d in decorators):
        return top
    budget = sum(d.count("\n") + 1 for d in decorators)
    found, first, i = 0, start, start - 1
    while i >= 0 and budget > 0 and lines[i].strip():
        if v1._DECORATOR_RE.match(lines[i]):
            found += 1
            first = i
            if found == len(decorators):
                return min(top, first)
        budget -= 1
        i -= 1
    return top


def _windows(first: int, last: int, size: int, overlap: int) -> list[tuple[int, int]]:
    """Overlapping line windows covering [first, last] completely, the last
    one ending at `last` however short it is (AUD-03)."""
    step = max(size - overlap, 1)
    out, start = [], first
    while True:
        end = min(start + size - 1, last)
        out.append((start, end))
        if end >= last:
            return out
        start += step


def _trim(lines: list[str], a: int, b: int) -> tuple[int, int] | None:
    while a <= b and not lines[a].strip():
        a += 1
    while b >= a and not lines[b].strip():
        b -= 1
    return (a, b) if a <= b else None


def _description(lines: list[str], unit: Any, lang: str, top: int) -> list[str]:
    """v1's own doc collection -- a comment banner above, and for Python the
    docstring read forward from the signature -- so both schemas describe a
    symbol identically and only the boundaries differ."""
    doc = v1._comment_banner(lines, top, lang)
    if lang == "python":
        first, last = unit.spans[0]
        doc += v1._python_docstring(lines, first - 1, min(last, len(lines)) - 1)
    return doc


def _chunk_lines(path: Path, rel_path: str, text: str, facts: Any,
                 lang: str | None) -> list[dict[str, Any]]:
    lines = _split_lines(text)
    n = len(lines)
    repo = rel_path.split("/", 1)[0] if "/" in rel_path else ""
    units = _units(facts.definitions, lines) if facts is not None else []
    uid = {u.qname: i for i, u in enumerate(units)}

    # 1. every line belongs to its innermost unit, or to the module (None)
    owner: list[int | None] = [None] * n
    for i, u in enumerate(units):                      # outermost first: inner overwrite
        for a, b in u.spans:
            for line in range(a - 1, min(b, n)):
                owner[line] = i

    # 2. decorators and the comment banner above a unit belong to it -- but
    #    only lines its enclosing region still holds, never a sibling's
    tops: dict[int, int] = {}
    for i, u in enumerate(units):
        enclosing = uid.get(u.parent) if u.parent is not None else None
        for k, (a, _b) in enumerate(u.spans):
            start = a - 1
            if start >= n:
                continue
            top = _decorated_start(lines, start, u.decorators if k == 0 else [])
            if k == 0:
                tops[i] = top
                top = v1._banner_start(lines, top, lang or "python")
            line = start - 1
            while line >= top and owner[line] == enclosing:
                owner[line] = i
                line -= 1

    # 3. segments: maximal runs of one owner; blank lines never split a run
    segments: list[list[Any]] = []
    for line in range(n):
        if not lines[line].strip():
            continue
        if segments and segments[-1][2] == owner[line]:
            segments[-1][1] = line
        else:
            segments.append([line, line, owner[line]])

    # 4. a fragment below the minimum joins a neighbour instead of being
    #    dropped. Neighbours are contiguous, so the result is still one slice
    #    of the file. Forward when the next run lies INSIDE the fragment's
    #    owner -- a bare `class Foo(Base):` header belongs with its first
    #    method, an import line with the definitions below it -- otherwise
    #    backward, so a trailing class attribute stays with its class.
    def tiny(seg: list[Any]) -> bool:
        return len("\n".join(lines[seg[0]:seg[1] + 1]).strip()) < v1.MIN_CHUNK_CHARS

    def inside(inner: int | None, outer: int | None) -> bool:
        if outer is None:
            return True
        while inner is not None:
            if inner == outer:
                return True
            parent = units[inner].parent
            inner = uid.get(parent) if parent is not None else None
        return False

    merged: list[list[Any]] = []
    carry: int | None = None
    for idx, seg in enumerate(segments):
        if carry is not None:
            seg[0], carry = carry, None
        if not tiny(seg):
            merged.append(seg)
            continue
        nxt = segments[idx + 1] if idx + 1 < len(segments) else None
        if nxt is not None and (inside(nxt[2], seg[2]) or not merged):
            carry = seg[0]
        elif merged:
            merged[-1][1] = seg[1]
        else:
            merged.append(seg)          # the file holds nothing else
    if len(merged) == 1 and tiny(merged[0]):
        return []

    # 5. windows: a unit by MAX_BODY_LINES, the remainder the v1 fallback way
    chunks: list[dict[str, Any]] = []
    docs: dict[int, list[str]] = {}
    for first, last, who in merged:
        if who is None:
            for a, b in _windows(first, last, v1.FALLBACK_WINDOW_LINES,
                                 v1.FALLBACK_OVERLAP_LINES):
                cut = _trim(lines, a, b)
                if cut is None:
                    continue
                chunks.append(v1._make_chunk(
                    rel_path=rel_path, repo=repo, kind="lines", name=f"lines_{cut[0] + 1}",
                    doc_lines=[], content="\n".join(lines[cut[0]:cut[1] + 1]),
                    line_start=cut[0] + 1, line_end=cut[1] + 1))
            continue
        unit = units[who]
        if who not in docs:
            docs[who] = _description(lines, unit, lang or "python", tops.get(who, unit.spans[0][0] - 1))
        local = unit.qname.split("::", 1)[-1]
        for a, b in _windows(first, last, v1.MAX_BODY_LINES, v1.FALLBACK_OVERLAP_LINES):
            cut = _trim(lines, a, b)
            if cut is None:
                continue
            chunk = v1._make_chunk(
                rel_path=rel_path, repo=repo, kind=unit.kind, name=local,
                doc_lines=docs[who], content="\n".join(lines[cut[0]:cut[1] + 1]),
                line_start=cut[0] + 1, line_end=cut[1] + 1)
            chunk["name"] = unit.name          # what `_compact_line` prints, as in v1
            chunk["qname"] = unit.qname
            chunks.append(chunk)
    for i, chunk in enumerate(chunks):
        chunk["chunk_id"] = f"{rel_path}#{i}"
    return chunks


def extract_code_chunks(path: Path, rel_path: str,
                        personal_roots: list[str] | None = None) -> list[dict[str, Any]]:
    """Chunk one source file at definition boundaries. Signature matches
    `Corpus.chunker`; `personal_roots` is ignored, as in v1.

    A grammar that cannot load raises `GrammarPinRefused` (the whole write
    stops before its commit). An extractor that fails on ONE file is a
    different thing -- a bug met on one input, not a parser that is missing --
    and that file is chunked as if it had no definitions, with a WARN naming it,
    rather than blocking the corpus until the bug is fixed."""
    try:
        data = path.read_bytes()
    except OSError:
        return []
    text = data.decode("utf-8", errors="ignore")
    if not text.strip():
        return []
    rel_path = rel_path.replace("\\", "/")
    lang = v1.LANG_BY_SUFFIX.get(path.suffix.lower())
    facts = None
    if lang is not None:
        extractor = load_extractors()[lang]
        try:
            facts = extractor.extract(rel_path, data)
        except Exception as exc:  # noqa: BLE001 -- see the docstring
            log.warning("[mb] WARNING: chunker v2 could not extract %s (%s: %s); "
                        "chunked without definitions", rel_path, type(exc).__name__, exc)
    return _chunk_lines(path, rel_path, text, facts, lang)
