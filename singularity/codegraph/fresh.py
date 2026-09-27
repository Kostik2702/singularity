"""Is the graph still the tree? The detection half of refresh-before-query
(CODE-GRAPH P3). Standard library only: on an unchanged tree a query costs
one `git status` and a few stats, imports no parser and writes nothing.

What the graph holds is a SNAPSHOT: the tree at `indexed_head` plus the
files that were dirty then (`dirty`, stored with the graph). A file whose
content on disk differs from the snapshot therefore differs from HEAD now
(git status lists it), or was changed by the commits between `indexed_head`
and HEAD (git diff lists it), or was one of the snapshot's dirty files
(`dirty` lists it). Those three sets are the candidates; each is compared
with its stored row by size and mtime, then by content hash, so a file git
lists but the graph already holds as it is costs one stat.

When `indexed_head` is gone (a shallow clone, a gc'd rebase) the diff
cannot be taken: every file of the unit is compared instead and the answer
says `head_unreachable`. A unit outside git is always compared whole.

A change to what DEFINES the unit's universe -- a `.gitignore`, a
`package.json` or `tsconfig*.json` (they steer TypeScript resolution), the
scope configuration, a nested repository appearing or going -- is not an
incremental change: `rebuild` names the reason and the caller rebuilds.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from ..search import code_chunking
from . import scope

WATCHED = scope._MANIFEST            # package.json / tsconfig*.json, by basename


def is_watched(path: str, git: bool = True) -> bool:
    """A file whose change is a rebuild. `.gitignore` only in a git unit:
    outside git it decides nothing (and the build does not record it)."""
    name = path.rsplit("/", 1)[-1]
    return (git and name == ".gitignore") or bool(WATCHED.match(name))


def watchable_path(path: str, git: bool, skip, vendored, nested: tuple[str, ...]) -> bool:
    """A file the build records in `watched`: the same prunes as its list
    (scope.list_manifests), so a manifest under node_modules or vendor, or a
    .gitignore outside git, can never look like one that "appeared" -- a unit
    outside git with a .gitignore would otherwise ask for a rebuild on every
    query (found on voice-text-recognition, 2026-09-27)."""
    parts = path.split("/")
    return (is_watched(path, git=git) and not any(scope._pruned(x, skip, vendored) for x in parts[:-1])
            and not any(path.startswith(n + "/") for n in nested))


def scope_fingerprint(cfg: Config) -> str:
    """Everything that decides which files a unit's graph holds. A graph
    built under another fingerprint is rebuilt, never patched."""
    vendored = scope._vendored(cfg)
    return hashlib.sha256(json.dumps([
        sorted(scope._skip_dirs(cfg)), vendored.pattern if vendored else None,
        list(scope.PRIVACY_PATTERNS), sorted(scope.CODE_SUFFIXES), code_chunking.MAX_FILE_BYTES,
        scope._TEST_NAME.pattern, sorted(scope._TEST_DIRS)]).encode()).hexdigest()[:16]


class GitFailed(RuntimeError):
    pass


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args],
                              capture_output=True)
    except FileNotFoundError as exc:
        raise GitFailed("git is not on PATH") from exc


@dataclass
class GitState:
    head: str | None              # None: a repository with no commit yet
    listed: set[str]              # every path `git status` names: modified, staged, both sides of a rename, untracked
    dirs: set[str]                # listed directories (`? inner/`): repositories nested in an untracked tree


def git_state(root: Path) -> GitState:
    """One `git status --porcelain=v2 --branch -uall`: HEAD and every path
    that differs from it, untracked files one by one."""
    done = _git(root, "status", "--porcelain=v2", "-z", "--branch", "-uall")
    if done.returncode != 0:
        raise GitFailed(f"git status failed in {root}: {done.stderr.decode(errors='replace').strip()}")
    head: str | None = None
    listed: set[str] = set()
    dirs: set[str] = set()
    records = done.stdout.split(b"\0")
    i = 0
    while i < len(records):
        r = records[i]
        i += 1
        if not r:
            continue
        kind = r[:2]
        if r.startswith(b"# branch.oid "):
            oid = r[len(b"# branch.oid "):].decode()
            head = None if oid == "(initial)" else oid
            continue
        if kind == b"1 ":
            path = r.split(b" ", 8)[8]
        elif kind == b"2 ":
            path = r.split(b" ", 9)[9]
            listed.add(records[i].decode("utf-8", "surrogateescape"))      # the rename's source
            i += 1
        elif kind == b"u ":
            path = r.split(b" ", 10)[10]
        elif kind == b"? ":
            path = r[2:]
        else:
            continue
        text = path.decode("utf-8", "surrogateescape")
        (dirs if text.endswith("/") else listed).add(text.rstrip("/"))
    return GitState(head, listed, dirs)


def diff_since(root: Path, base: str, head: str) -> set[str] | None:
    """Paths the commits base..head touched; None when `base` is gone."""
    done = _git(root, "diff", "--name-only", "--no-renames", "-z", base, head, "--")
    if done.returncode != 0:
        return None
    return {p.decode("utf-8", "surrogateescape") for p in done.stdout.split(b"\0") if p}


def in_universe(root: Path, paths: list[str]) -> set[str]:
    """Which of `paths` git lists as tracked or untracked-not-ignored -- the
    full build's universe. Asked only for paths no status line vouches for;
    in chunks, as a Windows command line ends at 32k characters."""
    out: set[str] = set()
    for i in range(0, len(paths), 100):
        done = _git(root, "ls-files", "-z", "-co", "--exclude-standard", "--", *paths[i:i + 100])
        if done.returncode != 0:
            raise GitFailed(f"git ls-files failed in {root}: {done.stderr.decode(errors='replace').strip()}")
        out |= {p.decode("utf-8", "surrogateescape") for p in done.stdout.split(b"\0") if p}
    return out


@dataclass
class Change:
    path: str
    kind: str                      # added | modified | deleted | touched (stat moved, content did not)
    size: int = 0
    mtime_ns: int = 0
    content_hash: str | None = None


# More candidates than this and they are not compared one by one: an update
# of that size is a background job anyway, and stat + hash of thousands of
# files would spend the query's budget on bookkeeping.
MAX_COMPARED = 2000


@dataclass
class Detection:
    head: str | None = None
    dirty: set[str] = field(default_factory=set)       # the snapshot's dirty set once these changes are applied
    changes: dict[str, Change] = field(default_factory=dict)
    rebuild: str | None = None                          # why an incremental update cannot be correct
    overflow: list[str] | None = None                   # candidates not compared (> MAX_COMPARED)
    head_unreachable: bool = False
    compared: int = 0                                   # files whose row was looked at

    @property
    def code_changes(self) -> list[Change]:
        return [c for c in self.changes.values() if c.kind != "touched"]

    def moves_snapshot(self, meta: dict[str, Any], stored_dirty: set[str]) -> bool:
        """HEAD or the dirty set moved: the snapshot's bookkeeping is due a
        write even when no file's content changed (after a commit, say --
        otherwise every later query pays for the diff again)."""
        return self.head != meta.get("indexed_head") or self.dirty != stored_dirty


def _hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _rows(con: sqlite3.Connection, paths: list[str] | None) -> dict[str, tuple[int, int, str]]:
    if paths is None:
        return {r[0]: (r[1], r[2], r[3]) for r in
                con.execute("SELECT path, size, mtime_ns, content_hash FROM files")}
    out: dict[str, tuple[int, int, str]] = {}
    for i in range(0, len(paths), 500):
        chunk = paths[i:i + 500]
        marks = ",".join("?" * len(chunk))
        out.update({r[0]: (r[1], r[2], r[3]) for r in con.execute(
            f"SELECT path, size, mtime_ns, content_hash FROM files WHERE path IN ({marks})", chunk)})
    return out


def _compare(root: Path, path: str, row: tuple[int, int, str] | None, present: bool,
             known: tuple[int, int] | None = None) -> Change | None:
    """The candidate against its stored row. `present`: the file is in the
    unit's universe now (exists, wanted, not too big, not ignored). `known`:
    its (size, mtime_ns) from a directory listing, instead of a stat."""
    if not present:
        return Change(path, "deleted") if row else None
    if known is None:
        try:
            st = (root / path).stat()                   # before any read: a write after it shows next time
        except OSError:
            return Change(path, "deleted") if row else None
        known = (st.st_size, st.st_mtime_ns)
    if row and row[0] == known[0] and row[1] == known[1]:
        return None
    digest = _hash(root / path)
    if digest is None:
        return Change(path, "deleted") if row else None
    if row is None:
        return Change(path, "added", known[0], known[1], digest)
    kind = "touched" if digest == row[2] else "modified"
    return Change(path, kind, known[0], known[1], digest)


def _present(root: Path, path: str, wanted) -> bool:
    if not wanted(path):
        return False
    try:
        st = (root / path).stat()
    except OSError:
        return False
    return st.st_size <= code_chunking.MAX_FILE_BYTES and os.path.isfile(root / path)


def _watched_changed(root: Path, paths: set[str], watched: dict[str, list]) -> str | None:
    for p in sorted(paths):
        rec = watched.get(p)
        try:
            st = (root / p).stat()
        except OSError:
            if rec is not None:
                return f"{p} was deleted"
            continue
        if rec is None:
            return f"{p} appeared"
        if rec[0] == st.st_size and rec[1] == st.st_mtime_ns:
            continue
        if _hash(root / p) != rec[2]:
            return f"{p} changed"
    return None


def detect(cfg: Config, meta: dict[str, Any], con: sqlite3.Connection,
           stored_dirty: set[str]) -> Detection:
    """Compare the unit's tree with the graph `con` holds. Reads only."""
    root = Path(meta["root"])
    nested = tuple(meta.get("nested") or ())
    if meta.get("scope") != scope_fingerprint(cfg):
        return Detection(rebuild="the scope configuration changed since the graph was built")
    skip, vendored = scope._skip_dirs(cfg), scope._vendored(cfg)

    def wanted(p: str) -> bool:
        return scope._wanted(p, skip, vendored, nested)

    is_git = bool(meta.get("is_git"))

    def watchable(p: str) -> bool:
        return watchable_path(p, is_git, skip, vendored, nested)

    watched: dict[str, list] = meta.get("watched") or {}
    det = Detection()
    if not is_git:
        files, found_nested = scope.walk_stat(root, skip, vendored)
        if sorted(found_nested) != sorted(nested):
            return Detection(rebuild="a nested repository appeared or went away")
        why = _watched_changed(root, {p for p in files if watchable(p)} | set(watched), watched)
        if why:
            return Detection(rebuild=why)
        _compare_all(root, con, {p: st for p, st in files.items()
                                 if wanted(p) and st[0] <= code_chunking.MAX_FILE_BYTES}, det)
        return det

    state = git_state(root)
    det.head = state.head
    det.dirty = {p for p in state.listed if wanted(p) or watchable(p)}
    for d in state.dirs:                   # `? inner/`: a repository inside an untracked tree
        if d not in nested and (root / d / ".git").exists() and not scope.is_linked_worktree(root / d) and not any(
                scope._pruned(part, skip, vendored) for part in d.split("/")):
            return Detection(rebuild=f"a repository appeared at {d}")
    if any(not (root / n / ".git").exists() for n in nested):
        return Detection(rebuild="a nested repository went away")
    candidates = set(state.listed) | stored_dirty
    base = meta.get("indexed_head")
    if base != state.head:
        # No base: the build saw a repository without commits, so every file
        # was untracked and is in the stored dirty set -- nothing to diff.
        since = diff_since(root, base, state.head) if base and state.head else None
        if since is not None:
            candidates |= since
        elif base is not None:
            det.head_unreachable = True
    why = _watched_changed(root, {p for p in candidates if watchable(p)} | (
        set(watched) if det.head_unreachable else set()), watched)
    if why:
        return Detection(rebuild=why)
    if det.head_unreachable:
        universe, _ = scope._git_entries(root)
        _compare_all(root, con, {p: None for p in universe if _present(root, p, wanted)}, det)
        return det
    code = sorted(p for p in candidates if wanted(p))
    if len(code) > MAX_COMPARED:
        det.overflow = code
        return det
    # a listed path is in the universe as git sees it; one only the diff or
    # the stored dirty set names may since have become ignored
    unvouched = [p for p in code if p not in state.listed and (root / p).exists()]
    vouched = set(state.listed) | in_universe(root, unvouched)
    rows = _rows(con, code)
    det.compared = len(code)
    for p in code:
        present = p in vouched and _present(root, p, wanted)
        ch = _compare(root, p, rows.get(p), present)
        if ch:
            det.changes[p] = ch
    return det


def _compare_all(root: Path, con: sqlite3.Connection, present: dict[str, tuple[int, int] | None],
                 det: Detection) -> None:
    """Every file of the unit against its row: stat for all (from the listing
    where there is one), a hash only where the stat moved. Outside git, and
    when HEAD's history is gone."""
    rows = _rows(con, None)
    det.compared = len(rows) + len(set(present) - set(rows))
    for p in sorted(set(present) | set(rows)):
        ch = _compare(root, p, rows.get(p), p in present, present.get(p))
        if ch:
            det.changes[p] = ch
    if len(det.code_changes) > MAX_COMPARED:
        det.overflow = sorted(c.path for c in det.code_changes)
        det.changes = {}
