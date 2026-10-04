"""Which repositories are graph units, and which of their files are read.

A UNIT is a git toplevel, or a directory under the repositories root that is
not in git at all. A repository nested inside another is its own unit and is
cut out of its parent: `git status` in the parent reports it as one opaque
`?? inner/` whatever changes inside it, so reading its files as the parent's
would index what the parent's change detector can never see.

A git unit's files are the ones git knows -- tracked plus untracked, minus
gitignored (`ls-files -co --exclude-standard`) -- so the file set and the
change detector agree by construction. On top of that the code corpus's own
filters apply (directory prunes, vendored distributions, privacy patterns,
the size cap), with one deliberate difference: tests and `.d.ts` files are
KEPT. The semantic corpus drops them as search noise; impact analysis needs
to name the test a change breaks, and TypeScript resolution needs the
declarations.

What is excluded because of one owner's circumstances (a former employer's
repository, an unpacked virtualenv) lives in `code_skip_dirs`, never in code.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from ..search import code_chunking

CODE_SUFFIXES = frozenset(p.lstrip("*") for p in code_chunking.CODE_PATTERNS)
PRIVACY_PATTERNS = ("*secret*", "*credential*")        # CODE-INDEX D5, layer two
_TEST_NAME = re.compile(r"(^test_.*\.py$|.*_test\.py$|^conftest\.py$|.*\.(test|spec)\.[jt]sx?$|.*\.d\.ts$)",
                        re.IGNORECASE)
# JUnit's and Android's naming, case-sensitive on purpose: `Edit.kt` is not an `IT`.
_JVM_TEST_NAME = re.compile(r".*(Test|Tests|IT)\.(java|kt)$")
_TEST_DIRS = {"tests", "test", "__tests__"}
_TEST_DIR_PREFIXES = ("androidTest",)       # androidTest, androidTestGplay, ...: a flavor's instrumented tests


class GraphError(RuntimeError):
    """The graph cannot be built as configured; the message says why."""


class GraphRootMissing(GraphError):
    """A configured root does not exist. Refused, never treated as empty: an
    unmounted drive must not read as "every file was deleted"."""


class IncompleteScan(GraphError):
    """Part of a unit could not be read. A build that went on would record
    the unreadable subtree as deleted, so it stops and changes nothing."""


@dataclass(frozen=True)
class Unit:
    key: str          # stable, path-safe id: root-relative, or `extra/<name>`
    root: Path
    is_git: bool
    nested: tuple[str, ...] = ()   # unit-relative dirs that are units of their own


@dataclass(frozen=True)
class FileEntry:
    path: str         # unit-relative, forward slashes
    size: int
    mtime_ns: int
    is_test: bool


def _skip_dirs(cfg: Config) -> frozenset[str]:
    return frozenset(code_chunking.SKIP_DIRS) | frozenset(cfg.data.get("code_skip_dirs") or ())


def _vendored(cfg: Config) -> re.Pattern[str] | None:
    raw = cfg.data.get("code_vendored_pattern")
    pattern = code_chunking.VENDORED_DIR_PATTERN if raw is None else raw
    return re.compile(pattern) if pattern else None


def _pruned(name: str, skip: frozenset[str], vendored: re.Pattern[str] | None) -> bool:
    return name in skip or bool(vendored and vendored.match(name))


def _is_git_root(path: Path) -> bool:
    return (path / ".git").exists()


def is_linked_worktree(path: Path) -> bool:
    """A `git worktree` checkout: its `.git` is a FILE pointing into another
    repository's `.git/worktrees/` (a submodule's points into `.git/modules/`).
    It is the same repository again -- neither a unit nor part of one. Agents
    and tools create them inside the repository they work on (Claude Code:
    `.claude/worktrees/agent-*`), and taking one for a nested repository made
    every refresh demand a rebuild while it existed."""
    marker = path / ".git"
    if not marker.is_file():
        return False
    try:
        head = marker.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return False
    return head.startswith("gitdir:") and "/worktrees/" in head.replace("\\", "/")


def _git(root: Path, *args: str) -> str:
    from ..platform.proc import NO_WINDOW

    try:
        done = subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              creationflags=NO_WINDOW)
    except FileNotFoundError as exc:
        raise GraphError("git is not on PATH; git units cannot be read without it") from exc
    if done.returncode != 0:
        raise GraphError(f"git {' '.join(args)} failed in {root}: {done.stderr.strip()}")
    return done.stdout


def _git_entries(root: Path) -> tuple[list[str], list[str]]:
    """(file paths git knows, unit-relative dirs that are nested repositories)."""
    files: list[str] = []
    nested: list[str] = []
    for record in _git(root, "ls-files", "-s", "-z").split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        (nested if meta.startswith("160000") else files).append(path)
    for path in _git(root, "ls-files", "-o", "--exclude-standard", "--directory", "-z").split("\0"):
        if not path:
            continue
        if path.endswith("/"):
            if is_linked_worktree(root / path):
                continue
            if _is_git_root(root / path):
                nested.append(path.rstrip("/"))
            else:
                more_files, more_nested = _walk_untracked(root, path.rstrip("/"))
                files.extend(more_files)
                nested.extend(more_nested)
        else:
            files.append(path)
    return files, nested


def _walk_untracked(root: Path, rel_dir: str) -> tuple[list[str], list[str]]:
    """`--directory` folds a wholly untracked directory into one entry -- and
    a repository nested in it disappears inside that entry (`?? plugins/`, not
    `?? plugins/inner/`). Its files are still this unit's; its nested
    repositories are still units of their own."""
    files: list[str] = []
    nested: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root / rel_dir):
        here = Path(dirpath)
        keep = []
        for d in dirnames:
            if is_linked_worktree(here / d):
                continue
            if _is_git_root(here / d):
                nested.append((here / d).relative_to(root).as_posix())
            else:
                keep.append(d)
        dirnames[:] = keep
        base = here.relative_to(root).as_posix()
        files.extend(f"{base}/{name}" for name in filenames)
    return files, nested


def _walk_plain(root: Path, skip: frozenset[str], vendored: re.Pattern[str] | None
                ) -> tuple[list[str], list[str]]:
    """(files, nested repositories) of a directory that is not in git."""
    files: list[str] = []
    nested: list[str] = []
    failures: list[str] = []
    seen: set[str] = set()

    def on_error(exc: OSError) -> None:
        failures.append(str(getattr(exc, "filename", "") or exc))

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in seen:                      # a link back to an ancestor
            dirnames[:] = []
            continue
        seen.add(real)
        here = Path(dirpath)
        keep = []
        for d in sorted(dirnames):
            if _pruned(d, skip, vendored) or is_linked_worktree(here / d):
                continue
            if _is_git_root(here / d):
                nested.append((here / d).relative_to(root).as_posix())
                continue
            keep.append(d)
        dirnames[:] = keep
        rel = here.relative_to(root).as_posix()
        files.extend(name if rel == "." else f"{rel}/{name}" for name in filenames)
    if failures:
        raise IncompleteScan(f"unreadable under {root}: {', '.join(sorted(failures)[:5])}")
    return files, nested


def _is_link(e: os.DirEntry) -> bool:
    """A symlink or (Windows) junction / other reparse point: its target can
    be anywhere, an ancestor included."""
    if e.is_symlink():
        return True
    attrs = getattr(e.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def walk_stat(root: Path, skip: frozenset[str], vendored: re.Pattern[str] | None
              ) -> tuple[dict[str, tuple[int, int]], list[str]]:
    """`_walk_plain` with each file's (size, mtime_ns) taken from the directory
    listing itself: on Windows a scandir entry carries both, where a stat per
    file costs a system call (B6: the refresh of a unit outside git walks the
    whole tree on every query). Same prunes, same nested repositories, same
    refusal of an unreadable subtree."""
    files: dict[str, tuple[int, int]] = {}
    nested: list[str] = []
    failures: list[str] = []
    seen: set[str] = set()
    # The real path of a plain directory is its parent's plus its name; only a
    # link or junction needs `realpath` -- which was half the walk's time.
    stack = [(str(root), "", os.path.realpath(root))]
    while stack:
        here, rel, real = stack.pop()
        try:
            entries = sorted(os.scandir(here), key=lambda e: e.name)
        except OSError as exc:
            failures.append(str(getattr(exc, "filename", "") or exc))
            continue
        # A repository is recognised from its own listing (a `.git` among its
        # entries), not by a stat of `<dir>/.git` for every directory walked;
        # like `_walk_plain`, every path that reaches one names it.
        if rel and any(e.name == ".git" for e in entries):
            if not is_linked_worktree(Path(here)):
                nested.append(rel.rstrip("/"))
            continue
        if real in seen:                                  # a link back to an ancestor, or walked already
            continue
        seen.add(real)
        # Children are visited in name order, depth first -- `_walk_plain`'s
        # order -- so when two paths reach one directory (a junction to a
        # sibling) the same one is kept, and the graph and its refresh agree.
        children = []
        for e in entries:
            try:
                if e.is_dir():
                    if _pruned(e.name, skip, vendored):
                        continue
                    child = (os.path.realpath(e.path) if _is_link(e)
                             else os.path.join(real, e.name))
                    children.append((e.path, f"{rel}{e.name}/", child))
                elif e.is_file():
                    st = e.stat()
                    files[rel + e.name] = (st.st_size, st.st_mtime_ns)
            except OSError as exc:
                failures.append(str(getattr(exc, "filename", "") or e.path))
        stack.extend(reversed(children))
    if failures:
        raise IncompleteScan(f"unreadable under {root}: {', '.join(sorted(failures)[:5])}")
    return files, nested


def _collect(root: Path, key: str, cfg: Config) -> list[Unit]:
    skip, vendored = _skip_dirs(cfg), _vendored(cfg)
    if _is_git_root(root):
        _, nested = _git_entries(root)
        nested = [n for n in nested
                  if not any(_pruned(part, skip, vendored) for part in n.split("/"))]
        is_git = True
    else:
        _, nested = _walk_plain(root, skip, vendored)
        is_git = False
    units = [Unit(key=key, root=root, is_git=is_git, nested=tuple(sorted(nested)))]
    for rel in sorted(nested):
        units.extend(_collect(root / rel, f"{key}/{rel}", cfg))
    return units


def _extra_key(root: Path, taken: set[str]) -> str:
    key = f"extra/{root.name}"
    if key in taken:
        key = f"{key}-{hashlib.sha256(str(root).encode()).hexdigest()[:8]}"
    return key


def discover_units(cfg: Config) -> list[Unit]:
    units: list[Unit] = []
    base = cfg.code_repos_dir
    if base is not None:
        if not base.is_dir():
            raise GraphRootMissing(f"code_repos_dir does not exist: {base}")
        skip, vendored = _skip_dirs(cfg), _vendored(cfg)
        for child in sorted(base.iterdir(), key=lambda p: p.name):
            if (not child.is_dir() or child.name in code_chunking.SKIP_REPOS
                    or _pruned(child.name, skip, vendored)):
                continue
            units.extend(_collect(child, child.name, cfg))
    taken = {u.key for u in units}
    for root in cfg.code_graph_extra_roots:
        if not root.is_dir():
            raise GraphRootMissing(f"code_graph_extra_roots entry does not exist: {root}")
        key = _extra_key(root, taken)
        taken.add(key)
        units.extend(_collect(root, key, cfg))
    return units


def is_test_path(path: str) -> bool:
    parts = path.split("/")
    return (bool(_TEST_NAME.match(parts[-1]) or _JVM_TEST_NAME.match(parts[-1]))
            or any(p in _TEST_DIRS or p.startswith(_TEST_DIR_PREFIXES) for p in parts[:-1]))


def _wanted(path: str, skip: frozenset[str], vendored: re.Pattern[str] | None,
            nested: tuple[str, ...]) -> bool:
    if os.path.splitext(path)[1].lower() not in CODE_SUFFIXES:
        return False
    parts = path.split("/")
    if any(_pruned(p, skip, vendored) for p in parts[:-1]):
        return False
    if any(path == n or path.startswith(n + "/") for n in nested):
        return False
    lowered = path.lower()
    return not any(fnmatch.fnmatchcase(lowered, pat) for pat in PRIVACY_PATTERNS)


_MANIFEST = re.compile(r"^(package\.json|tsconfig[\w.-]*\.json)$")


def list_manifests(unit: Unit, cfg: Config, include_gitignore: bool = False) -> list[str]:
    """`package.json` and `tsconfig*.json` files the TypeScript resolver reads
    to map `@scope/pkg` specifiers and path aliases onto files of this unit
    (and, asked for, the `.gitignore` files that shape its universe).
    Same prunes as code files: a node_modules manifest is not ours."""
    skip, vendored = _skip_dirs(cfg), _vendored(cfg)
    if unit.is_git:
        candidates, _ = _git_entries(unit.root)
    else:
        candidates, _ = _walk_plain(unit.root, skip, vendored)
    out = []
    for path in sorted(set(candidates)):
        parts = path.split("/")
        if not (_MANIFEST.match(parts[-1]) or (include_gitignore and parts[-1] == ".gitignore")) or any(
                _pruned(p, skip, vendored) for p in parts[:-1]):
            continue
        if any(path.startswith(n + "/") for n in unit.nested):
            continue
        out.append(path)
    return out


def list_files(unit: Unit, cfg: Config) -> list[FileEntry]:
    """The unit's files the graph reads, sorted, with the stat taken now."""
    skip, vendored = _skip_dirs(cfg), _vendored(cfg)
    if unit.is_git:
        candidates, _ = _git_entries(unit.root)
    else:
        candidates, _ = _walk_plain(unit.root, skip, vendored)
    out: list[FileEntry] = []
    for path in sorted(set(candidates)):
        if not _wanted(path, skip, vendored, unit.nested):
            continue
        try:
            st = (unit.root / path).stat()
        except OSError:
            continue                     # tracked by git but deleted in the worktree
        if st.st_size > code_chunking.max_file_bytes(path):
            continue
        out.append(FileEntry(path=path, size=st.st_size, mtime_ns=st.st_mtime_ns,
                             is_test=is_test_path(path)))
    return out
