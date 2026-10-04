"""`mb code impact`: what a change touches (CODE-GRAPH P4).

The change is the diff between a base and the working tree: uncommitted
work against HEAD by default, a branch against the merge-base with `--base`.
Its changed lines, mapped to the innermost definitions that hold them, are
the changed symbols; the base revision of every changed file is parsed in
memory, so a symbol deleted or renamed by the change is seen too, not only
the ones still in the tree. From the changed symbols the callers are walked
back (calls, subclasses; importers for a module-level change) to `--depth`.

The graph must hold the whole diff before the walk means anything, so the
command first brings it up to date -- the normal refresh, then, if files of
the diff are still stale, a full update of them -- or refuses and says how
many files it would take (decision D1, "impact updates the whole diff or
refuses with the count"). The answer carries its coverage: which changed
files the graph holds, which it does not, and how many call sites of the
changed names the resolver could not place (possible callers the walk
cannot see).
"""
from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import fresh, languages
from .query import page

IMPACT_MAX_FILES = 2000
_HUNK = re.compile(rb"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


class ImpactRefused(RuntimeError):
    pass


def _git(root: Path, *args: str) -> bytes:
    done = fresh._git(root, *args)
    if done.returncode != 0:
        raise fresh.GitFailed(f"git {' '.join(args[:3])} failed in {root}: "
                              f"{done.stderr.decode(errors='replace').strip()}")
    return done.stdout


def resolve_base(root: Path, base: str | None) -> tuple[str, str]:
    """(commit, what it is). No base: HEAD, i.e. the uncommitted change."""
    if base is None:
        return _git(root, "rev-parse", "HEAD").decode().strip(), "HEAD: the uncommitted change"
    sha = _git(root, "merge-base", base, "HEAD").decode().strip()
    return sha, f"merge-base of {base} and HEAD"


def changed_files(root: Path, base_sha: str) -> dict[str, str]:
    """{path: A | M | D} between the base and the working tree, untracked files as A."""
    out: dict[str, str] = {}
    fields = _git(root, "diff", "--name-status", "-z", "--no-renames", base_sha).split(b"\0")
    for status, path in zip(fields[0::2], fields[1::2], strict=False):
        if status and path:
            out[path.decode("utf-8", "surrogateescape")] = status.decode()[0]
    for path in _git(root, "ls-files", "-z", "-o", "--exclude-standard").split(b"\0"):
        if path:
            out.setdefault(path.decode("utf-8", "surrogateescape"), "A")
    return out


def changed_lines(root: Path, base_sha: str, paths: list[str]) -> dict[str, set[int]]:
    """New-side line numbers each file's diff touches. A pure deletion marks
    the line it happened at, so the definition that lost lines is found."""
    out: dict[str, set[int]] = defaultdict(set)
    for i in range(0, len(paths), 100):
        current: str | None = None
        for line in _git(root, "diff", "-U0", "--no-renames", base_sha, "--", *paths[i:i + 100]).split(b"\n"):
            if line.startswith(b"+++ "):
                target = line[4:].decode("utf-8", "surrogateescape")
                current = target[2:] if target.startswith("b/") else None
            elif current and (m := _HUNK.match(line)):
                start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
                out[current].update(range(start, start + count) if count else (start, start + 1))
    return out


@dataclass
class _Def:
    qname: str
    name: str
    kind: str
    spans: list[tuple[int, int]]
    body: str                         # the text below the definition line, whitespace-normalised


def _body(text_lines: list[str], spans: list[tuple[int, int]]) -> str:
    a, b = spans[0]
    return " ".join(" ".join(text_lines[a:b]).split())      # lines a+1..b: the first line holds the name


def _defs_of(path: str, data: bytes) -> dict[str, _Def]:
    _lang, facts = languages.extract(path, data)     # the parser: impact parses the base revision
    lines = data.decode("utf-8", "replace").split("\n")
    return {d.qname: _Def(d.qname, d.name, d.kind, [tuple(s) for s in d.spans], _body(lines, d.spans))
            for d in facts.definitions}


def _innermost(defs: list[tuple[str, list[tuple[int, int]]]], line: int) -> str | None:
    best, best_len = None, None
    for q, spans in defs:
        for a, b in spans:
            if a <= line <= b and (best_len is None or b - a < best_len):
                best, best_len = q, b - a
    return best


def impact(con: sqlite3.Connection, root: Path, base: str | None, depth: int = 3,
           limit: int = 100, offset: int = 0) -> dict[str, Any]:
    base_sha, base_what = resolve_base(root, base)
    files = changed_files(root, base_sha)
    in_graph = {r[0]: (r[1], bool(r[2])) for r in con.execute("SELECT path, lang, is_test FROM files")}
    code = sorted(p for p in files if p in in_graph or (files[p] == "D" and languages.lang_of(p)))
    outside = sorted(p for p in files if p not in code)
    lines = changed_lines(root, base_sha, [p for p in code if files[p] != "D"])

    changed: list[dict[str, Any]] = []
    sources: set[str] = set()
    gone_names: dict[str, str] = {}                # deleted (not renamed) name -> its language
    for path in code:
        now: dict[str, _Def] = {}
        if files[path] != "D":
            text = (root / path).read_bytes()
            now = _defs_of(path, text)
        before: dict[str, _Def] = {}
        if files[path] != "A":
            try:
                before = _defs_of(path, _git(root, "show", f"{base_sha}:{path}"))
            except fresh.GitFailed:
                before = {}
        deleted = {q: d for q, d in before.items() if q not in now}
        added = {q: d for q, d in now.items() if q not in before}
        renamed: dict[str, str] = {}
        for oq, od in deleted.items():
            # the same body under another name; an empty body (a one-liner) proves nothing
            twin = next((nq for nq, nd in added.items() if od.body and nd.kind == od.kind
                         and nd.body == od.body and nq not in renamed.values()), None)
            if twin:
                renamed[oq] = twin
        lang = languages.lang_of(path)
        for oq, nq in renamed.items():
            changed.append({"symbol": nq, "change": "renamed", "was": oq, "file": path})
            sources.add(nq)
            gone_names[deleted[oq].name] = lang
        for oq, d in deleted.items():
            if oq not in renamed:
                changed.append({"symbol": oq, "change": "deleted", "file": path})
                gone_names[d.name] = lang
        for nq in added:
            if nq not in renamed.values():
                changed.append({"symbol": nq, "change": "added", "file": path})
                sources.add(nq)
        spans = [(q, d.spans) for q, d in now.items()]
        touched: set[str] = set()
        module_level = False
        for ln in sorted(lines.get(path, ())):
            q = _innermost(spans, ln)
            if q is None:
                module_level = True
            elif q not in added and q not in renamed.values():
                touched.add(q)
        for q in sorted(touched):
            changed.append({"symbol": q, "change": "modified", "file": path})
            sources.add(q)
        if module_level and files[path] != "D":
            changed.append({"symbol": f"{path}::<module>", "change": "module-level", "file": path})
            sources.add(f"{path}::<module>")

    # walk the callers back
    reached: dict[str, tuple[int, str]] = {}
    frontier = sorted(sources)
    for d in range(1, depth + 1):
        nxt: list[str] = []
        for i in range(0, len(frontier), 500):
            chunk = frontier[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for src, dst, kind in con.execute(
                    f"SELECT src, dst, kind FROM edges WHERE dst IN ({marks}) AND kind IN ('CALLS', 'INHERITS')"
                    f" UNION ALL SELECT src, dst, kind FROM edges WHERE kind = 'IMPORTS' AND dst IN ({marks})",
                    [*chunk, *chunk]):
                if src in sources or src in reached:
                    continue
                reached[src] = (d, f"{'calls' if kind == 'CALLS' else kind.lower()} {dst}")
                nxt.append(src)
        frontier = sorted(set(nxt))
        if not frontier:
            break

    def file_of(q: str) -> str:
        return q.split("::", 1)[0]
    impacted = [{"symbol": q, "depth": d, "via": via, "file": file_of(q),
                 "test": in_graph.get(file_of(q), ("", False))[1]}
                for q, (d, via) in sorted(reached.items(), key=lambda x: (x[1][0], x[0]))]
    tests = sorted({i["file"] for i in impacted if i["test"]}
                   | {p for p in code if in_graph.get(p, ("", False))[1]})
    broken = [{"name": n, "caller": src, "file": path, "line": line} for n, lang in sorted(gone_names.items())
              for src, path, line in con.execute(
                  "SELECT u.src, f.path, u.line FROM unresolved u JOIN files f ON f.id = u.file_id"
                  " WHERE u.name = ? AND f.lang = ? ORDER BY f.path, u.line", (n, lang))]
    names = sorted({s["symbol"].split("::", 1)[1].split(".")[-1] for s in changed
                    if not s["symbol"].endswith("<module>")})
    unplaced = sum(con.execute("SELECT count(*) FROM unresolved WHERE name = ?", (n,)).fetchone()[0]
                   for n in names)
    return {
        "base": {"commit": base_sha, "what": base_what},
        "changed": {"symbols": changed, "files": {p: files[p] for p in code}},
        "impacted": page(impacted[offset:offset + limit], len(impacted), offset, limit),
        "tests": tests,
        "possibly_broken": broken,
        "coverage": {"changed_files": len(files), "code_files": len(code), "outside_graph": outside,
                     "depth": depth, "unresolved_same_name_sites": unplaced,
                     "note": "callers the resolver could not place are not walked; "
                             "`unresolved_same_name_sites` counts the call sites that may be missing"}}
