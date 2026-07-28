"""Symbol-boundary chunking for the code corpus (C02).

Regex boundaries, deliberately NOT an AST (decision in the epic §1): a parser
per language is a dependency, a version treadmill and a hard failure on the
first file that does not parse, in exchange for maybe 20% better boundaries.
A regex degrades into fixed-size windows instead of failing, which is the
behavior we want on a repositories root full of half-finished scratch files.

Quality of a code hit is decided by what lands in the chunk. A whole file is
useless (one embedding for 600 lines of unrelated symbols) and a single line is
useless (no context); the unit is ONE SYMBOL with its signature and its
docstring. Numbers below are ported from the Genevra reverse-engineering write
-up (vault: Semantic_Search_Deep_Dive §5.2).

Languages (decision D6 — Python is primary here, the inverse of Genevra):
  * Python   — 69% of this codebase and entirely new code; Genevra ran `.py`
               through its TS chunker, where it fell into fallback almost every
               time. Triple-quoted docstrings are read FORWARD from the
               signature, which is where Python puts them, unlike a `#` banner
               above.
  * TS/JS/JSX— ported boundaries.
  * Ruby     — NOT implemented: zero `.rb` files exist here. `.rb` is not
               claimed as supported and simply lands in the fallback windower
               rather than being cut on invented boundaries.

Two deliberate deviations from the epic's literal text, both forced by the
surrounding code rather than chosen:

  1. The embedded string is `"{file} — {kind} {name}: {doc} | {code}"`, not
     `"{repo}/{file} — ..."`. `file` here is the CORPUS-root-relative path and
     already begins with the repo segment (`indexer._scan_corpus` produces the
     key, and `_update`'s keep-mask matches chunks by exactly that key), so
     prepending `repo` again would embed `api/api/app.py`.
  2. `line_end` is the last line actually INCLUDED after body truncation, not
     the symbol's real last line. That makes the pair (line_start, line_end)
     verifiable — reading those lines back out of the file reproduces
     `content` — which is the property the acceptance test checks.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

# Bumped whenever the code chunk shape changes. Per-corpus by construction
# (D3): this is NOT `chunking.CHUNK_SCHEMA_VERSION`, so bumping the markdown
# chunker cannot force a rebuild of the code index and vice versa.
CODE_CHUNK_SCHEMA_VERSION = 1

MAX_BODY_LINES = 60          # symbol body cut; a longer symbol is a design smell
DOC_LOOKBACK_LINES = 7       # comment banner above the signature
DOC_DESCRIPTION_LINES = 4    # of the collected doc, what becomes `description`
MIN_CHUNK_CHARS = 30         # below this a "symbol" is a stub or a re-export
FALLBACK_WINDOW_LINES = 40
FALLBACK_OVERLAP_LINES = 10
FALLBACK_MIN_CHARS = 50
EMBED_CODE_CHARS = 250       # path + signature + doc must outweigh the body

LANG_BY_SUFFIX = {
    ".py": "python",
    ".ts": "ts", ".tsx": "ts", ".js": "ts", ".jsx": "ts",
}

# --------------------------------------------------------------------------
# What the code corpus collects (C03). Constants, NOT config keys: these lists
# are long, they change with the ecosystem rather than with the user, and
# nobody is going to curate thirty directory names through `mb config set`.
# The one thing that IS configuration is WHERE to look — `code_repos_dir`.
# --------------------------------------------------------------------------

CODE_PATTERNS = ("*.py", "*.ts", "*.tsx", "*.js", "*.jsx")   # D6: no *.rb

# Cut during traversal, at any depth. This is the single highest-leverage
# filter in the whole feature: on a measured repositories root it removed ~99%
# of extension-matching candidates, and it does so without ever entering the
# subtree (see indexer._scan_corpus).
SKIP_DIRS = (
    ".git", ".hg", ".svn",
    "node_modules", "bower_components", "vendor", "third_party",
    ".venv", "venv", "env", "virtualenv", "site-packages",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".eggs",
    "dist", "build", "out", "target", ".next", ".nuxt", ".svelte-kit",
    ".turbo", ".parcel-cache", ".cache", ".gradle", ".terraform",
    "coverage", "htmlcov", "storybook-static", "__snapshots__", "__tests__",
    "Pods", "DerivedData", ".idea", ".vscode", ".mb",
    # Sphinx's convention for bundled third-party assets. Found by the C06 run
    # on a real root: a vendored `doc/_static/underscore-*.js` was ranking first
    # for "cosine similarity" — a jQuery-era utility library, indexed only
    # because it happened to sit inside a documentation folder.
    "_static",
)

# Matched against the whole corpus-relative path, case-insensitively.
SKIP_FILE_PATTERNS = (
    "*.test.*", "*.spec.*",              # JS/TS test convention
    "*.min.js", "*.min.css", "*.bundle.js", "*.map",
    "*.d.ts",                            # type declarations: signatures, no logic
    "*/migrations/*", "*_pb2.py", "*_pb2_grpc.py", "*.generated.*",
    # CODE-INDEX D5, layer two. Layer one is `CODE_PATTERNS` itself, which
    # excludes `.env`/`.pem`/dumps by construction; layer three is the grep of
    # the built index in C06, the only layer that checks a fact instead of a
    # hypothesis about where secrets live.
    "*secret*", "*credential*",
)

# Repositories excluded wholesale (CODE-INDEX D1): a fixture tree and a vendored
# copy of the system this one replaced. Erring toward "something extra got
# indexed" is deliberate — that failure is visible in search results and fixed
# by one line, whereas an allowlist fails silently by omission.
SKIP_REPOS = ("test-project", "genevra-portable")

# A source file past this is generated, vendored or a data blob pretending to be
# code; embedding it costs real time and returns noise.
MAX_FILE_BYTES = 100_000

_PY_BOUNDARY_RE = re.compile(r"^([ \t]*)(?:async\s+)?(def|class)\s+(\w+)")
_TS_BOUNDARY_RE = re.compile(
    r"^[ \t]*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:abstract\s+)?"
    r"(class|interface|function|enum)\s+(\w+)")
_TS_EXPORT_CONST_RE = re.compile(
    r"^[ \t]*export\s+(?:const|let|var)\s+(\w+)\s*[=:(]")
_DECORATOR_RE = re.compile(r"^[ \t]*@\w")
_PY_COMMENT_RE = re.compile(r"^[ \t]*#")
_TS_COMMENT_RE = re.compile(r"^[ \t]*(?://|/\*|\*)")
_DOC_NOISE_RE = re.compile(r'^[ \t]*(?:#+|//+|/\*+|\*+/?|"""|\'\'\')\s?')
_TRAIL_NOISE_RE = re.compile(r'(?:\*/|"""|\'\'\')\s*$')
_WS_RE = re.compile(r"\s+")


def _clean_doc_line(line: str) -> str:
    line = _DOC_NOISE_RE.sub("", line)
    line = _TRAIL_NOISE_RE.sub("", line)
    return line.strip()


def _comment_banner(lines: list[str], start: int, lang: str) -> list[str]:
    """Comment lines immediately ABOVE `start`, nearest-last, at most
    DOC_LOOKBACK_LINES. Decorator lines are transparent: a `#` banner sitting
    above `@property` still describes the method underneath it."""
    is_comment = _PY_COMMENT_RE if lang == "python" else _TS_COMMENT_RE
    out: list[str] = []
    i = start - 1
    while i >= 0 and len(out) < DOC_LOOKBACK_LINES:
        if _DECORATOR_RE.match(lines[i]):
            i -= 1
            continue
        if not is_comment.match(lines[i]):
            break
        text = _clean_doc_line(lines[i])
        if text:
            out.append(text)
        i -= 1
    out.reverse()
    return out


def _banner_start(lines: list[str], start: int, lang: str) -> int:
    """First line of the comment banner above `start`, else `start` itself.

    Used to end the PREVIOUS chunk one line earlier: without it, the `// Renders
    one widget` comment describing the next symbol is glued onto the tail of the
    symbol before it, embedding text that belongs to a different thing."""
    is_comment = _PY_COMMENT_RE if lang == "python" else _TS_COMMENT_RE
    i, first = start - 1, start
    seen = 0
    while i >= 0 and seen < DOC_LOOKBACK_LINES:
        if _DECORATOR_RE.match(lines[i]):
            i -= 1
            continue
        if not is_comment.match(lines[i]):
            break
        first = i
        seen += 1
        i -= 1
    return first


def _python_docstring(lines: list[str], start: int, end: int) -> list[str]:
    """The `\"\"\"...\"\"\"` block directly under the signature.

    The signature may wrap over several lines, so the end of it is found first
    (the line whose code part ends with `:`) instead of assuming `start + 1`."""
    sig_end = None
    for i in range(start, min(end, start + 10) + 1):
        code = lines[i].split("#", 1)[0].rstrip()
        if code.endswith(":"):
            sig_end = i
            break
    if sig_end is None:
        return []

    i = sig_end + 1
    while i <= end and not lines[i].strip():
        i += 1
    if i > end:
        return []
    stripped = lines[i].lstrip()
    quote = '"""' if stripped.startswith('"""') else (
        "'''" if stripped.startswith("'''") else None)
    if quote is None:
        return []

    body = stripped[3:]
    if quote in body:                                   # one-line docstring
        return [t for t in [_clean_doc_line(body.split(quote, 1)[0])] if t]

    out = [t for t in [_clean_doc_line(body)] if t]
    i += 1
    while i <= end and len(out) < DOC_LOOKBACK_LINES:
        if quote in lines[i]:
            tail = _clean_doc_line(lines[i].split(quote, 1)[0])
            if tail:
                out.append(tail)
            break
        text = _clean_doc_line(lines[i])
        if text:
            out.append(text)
        i += 1
    return out


def _boundaries(lines: list[str], lang: str) -> list[tuple[int, str, str]]:
    """(line_index, kind, name) for every symbol start, in file order."""
    found: list[tuple[int, str, str]] = []
    for i, line in enumerate(lines):
        if lang == "python":
            m = _PY_BOUNDARY_RE.match(line)
            if not m:
                continue
            indent, keyword, name = m.group(1), m.group(2), m.group(3)
            kind = "class" if keyword == "class" else (
                "method" if indent else "function")
            found.append((i, kind, name))
            continue
        m = _TS_BOUNDARY_RE.match(line)
        if m:
            found.append((i, m.group(1), m.group(2)))
            continue
        m = _TS_EXPORT_CONST_RE.match(line)
        if m:
            # covers `export const x = () => {}` (an arrow function) and
            # `export const CONFIG = {...}` alike -- both are the module's
            # public surface, which is what a code search asks about
            found.append((i, "const", m.group(1)))
    return found


def _extend_over_decorators(lines: list[str], start: int) -> int:
    """Pull contiguous `@decorator` lines into the chunk below them. A chunk
    that begins at `def handler` while `@app.route("/x")` sits one line above
    loses the single most searchable fact about that function."""
    i = start
    while i > 0 and _DECORATOR_RE.match(lines[i - 1]):
        i -= 1
    return i


def _fixed_chunks(lines: list[str], rel_path: str, repo: str) -> list[dict[str, Any]]:
    """No recognizable symbol boundary: config objects, bare scripts, `.rb`.
    Overlapping windows are honest about knowing nothing about the structure."""
    out: list[dict[str, Any]] = []
    step = max(FALLBACK_WINDOW_LINES - FALLBACK_OVERLAP_LINES, 1)
    for start in range(0, len(lines), step):
        window = lines[start:start + FALLBACK_WINDOW_LINES]
        content = "\n".join(window).strip("\n")
        if len(content.strip()) < FALLBACK_MIN_CHARS:
            continue
        out.append(_make_chunk(
            rel_path=rel_path, repo=repo, kind="lines",
            name=f"lines_{start + 1}", doc_lines=[], content=content,
            line_start=start + 1, line_end=start + len(window)))
        if start + FALLBACK_WINDOW_LINES >= len(lines):
            break
    return out


def _make_chunk(*, rel_path: str, repo: str, kind: str, name: str,
                doc_lines: list[str], content: str,
                line_start: int, line_end: int) -> dict[str, Any]:
    description = _WS_RE.sub(" ", " ".join(doc_lines[:DOC_DESCRIPTION_LINES])).strip()
    flat = _WS_RE.sub(" ", content).strip()
    # path and signature first, body last and capped: a query is far more often
    # "where is retry handled" than a literal fragment of the body
    head = f"{rel_path} — {kind} {name}"
    if description:
        head = f"{head}: {description}"
    chunk: dict[str, Any] = {
        "file": rel_path,
        "kind": kind,
        "name": name,
        "content": content,
        "line_start": line_start,
        "line_end": line_end,
        "text": f"{head} | {flat[:EMBED_CODE_CHARS]}",
    }
    if repo:
        chunk["repo"] = repo
    if description:
        chunk["description"] = description
    return chunk


def extract_code_chunks(path: Path, rel_path: str,
                        personal_roots: Optional[list[str]] = None
                        ) -> list[dict[str, Any]]:
    """Chunk one source file. Signature matches `Corpus.chunker`.

    `personal_roots` is accepted and ignored: it classifies VAULT notes as
    personal, and a code corpus has no such notion. Dropping the parameter
    instead would make this chunker incompatible with the shared writer."""
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    if not raw.strip():
        return []

    lines = raw.splitlines()
    rel_path = rel_path.replace("\\", "/")
    repo = rel_path.split("/", 1)[0] if "/" in rel_path else ""
    lang = LANG_BY_SUFFIX.get(path.suffix.lower())

    if lang is None:
        return _fixed_chunks(lines, rel_path, repo)

    found = _boundaries(lines, lang)
    if not found:
        return _fixed_chunks(lines, rel_path, repo)

    # decorators first, so a chunk never ends inside the next symbol's decorators
    starts = [_extend_over_decorators(lines, idx) for idx, _, _ in found]
    # ...and the next symbol's comment banner belongs to IT, not to the tail of
    # the symbol above it
    cuts = [_banner_start(lines, s, lang) for s in starts]
    chunks: list[dict[str, Any]] = []
    for pos, (sig_idx, kind, name) in enumerate(found):
        start = starts[pos]
        hard_end = (cuts[pos + 1] - 1) if pos + 1 < len(starts) else len(lines) - 1
        if hard_end < start:
            continue                       # stacked boundaries (decorator soup)
        end = min(hard_end, start + MAX_BODY_LINES - 1)

        doc = _comment_banner(lines, start, lang)
        if lang == "python":
            doc += _python_docstring(lines, sig_idx, hard_end)

        content = "\n".join(lines[start:end + 1]).strip("\n")
        if len(content.strip()) < MIN_CHUNK_CHARS:
            continue
        chunks.append(_make_chunk(
            rel_path=rel_path, repo=repo, kind=kind, name=name, doc_lines=doc,
            content=content, line_start=start + 1, line_end=end + 1))

    if not chunks:
        return _fixed_chunks(lines, rel_path, repo)
    for i, chunk in enumerate(chunks):
        chunk["chunk_id"] = f"{rel_path}#{i}"
    return chunks
