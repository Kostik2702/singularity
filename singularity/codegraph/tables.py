"""What a resolver reads, one file at a time, and what each resolved file read
(CODE-GRAPH P3).

A resolver used to index every file of the unit up front. An incremental
update cannot afford that -- a 100k-file unit has to re-resolve a handful of
files in well under two seconds -- and it cannot be correct without knowing
which files a resolution depended on. So the resolvers read through
`Tables`: a file's definitions, bindings, hints and exports are indexed the
first time any of them is asked for, and every read of ANOTHER file's entry,
every existence probe of a module path (a miss included: a file created at
that path later changes the answer), and every unique-name lookup is
recorded against the file being resolved. The update re-resolves exactly
the files whose recorded reads touch what changed.

Two rules keep the record complete:
* every memo table that spans files is cleared per resolved file, so a hit
  is always something this file's own resolution computed -- and recorded;
* name-level answers (`candidates`) may be shared across files: they depend
  on the name alone, and the name is recorded on every call, hit or miss.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import Any, NamedTuple

from . import languages
from .model import Definition, FileFacts

_EMPTY: dict = {}          # the answer for a scope with no entries; never written to


class Candidate(NamedTuple):
    """A definition as the unique-name tier sees it."""
    qname: str
    kind: str
    local: bool            # defined inside a function: unreachable by name from elsewhere
    path: str
    is_test: bool


def is_local(defs: dict[str, Definition], qname: str, containers: Iterable[str]) -> bool:
    """Some enclosing definition is not a container (a class, and in TypeScript
    also an interface, type or enum): the definition lives in a function."""
    q = defs[qname].parent
    while q and q in defs:
        if defs[q].kind not in containers:
            return True
        q = defs[q].parent
    return False


class MemorySource:
    """Every file's facts in memory: the full build."""

    def __init__(self, facts: dict[str, FileFacts], test_paths: Iterable[str], containers: Iterable[str]):
        self._facts = facts
        self._tests = frozenset(test_paths)
        containers = tuple(containers)
        by_name: dict[str, list[Candidate]] = defaultdict(list)
        by_package: dict[str, list[str]] = defaultdict(list)
        tops: dict[tuple[str, str], list[str]] = defaultdict(list)
        facades: dict[tuple[str, str], list[str]] = defaultdict(list)
        for path, f in sorted(facts.items()):
            defs = {d.qname: d for d in f.definitions}
            for d in f.definitions:
                by_name[d.name].append(Candidate(d.qname, d.kind, is_local(defs, d.qname, containers),
                                                 path, path in self._tests))
            if languages.family(f.lang).packages:
                package = f.package or ""
                by_package[package].append(path)
                for d in f.definitions:
                    if d.parent is None:
                        tops[(package, d.name)].append(d.qname)
                if (fac := languages.facade(f)) is not None:
                    facades[(package, fac)].append(path)
        self._by_name = {k: tuple(v) for k, v in by_name.items()}
        self._by_package = {k: tuple(v) for k, v in by_package.items()}
        self._tops = {k: tuple(v) for k, v in tops.items()}
        self._facades = {k: tuple(v) for k, v in facades.items()}

    # The JVM family finds a type through its package, not its file's path.
    # '' is the default package.

    def package_files(self, package: str) -> tuple[str, ...]:
        """The files declaring `package`, sorted."""
        return self._by_package.get(package, ())

    def top_level(self, package: str, name: str) -> tuple[str, ...]:
        """Qnames of the top-level definitions called `name` in `package`'s
        files, sorted by path: several when build flavors each define it."""
        return self._tops.get((package, name), ())

    def facades(self, package: str, name: str) -> tuple[str, ...]:
        """The Kotlin files of `package` Java sees as the class `name`."""
        return self._facades.get((package, name), ())

    def paths(self) -> list[str]:
        return list(self._facts)

    def exists(self, path: str) -> bool:
        return path in self._facts

    def facts(self, path: str) -> FileFacts:
        return self._facts[path]

    def is_test(self, path: str) -> bool:
        return path in self._tests

    def candidates(self, name: str) -> tuple[Candidate, ...]:
        return self._by_name.get(name, ())

    def guess(self, name: str, from_test: bool, never: tuple[str, ...]) -> str | None:
        return guess_target(self.candidates(name), from_test, never)


def guess_target(cands: Iterable[Candidate], from_test: bool, never: tuple[str, ...] = ()) -> str | None:
    """The unique-name tier's answer from a name's definitions: the one that
    is neither local to a function nor a test's (unless the caller is a
    test) nor of a kind in `never`, or None. `update.DbSource.guess` is the
    same rule in SQL; the oracle holds the two together."""
    hits = [c.qname for c in cands
            if not c.local and (from_test or not c.is_test) and c.kind not in never]
    return hits[0] if len(hits) == 1 else None


class _View:
    """A dict keyed by qname or scope (`path::...`) or by path, whose file is
    indexed on first touch and recorded as a dependency of the current file.
    `empty` set: a missing key reads as that (a scope with no entries)."""
    __slots__ = ("_t", "_d", "_empty")

    def __init__(self, t: Tables, d: dict, empty: Any = None):
        self._t, self._d, self._empty = t, d, empty

    # A key may be None (`c.klass` of a call outside any class): no file, a miss.

    def __getitem__(self, key: str | None):
        if key is not None:
            self._t.see(key.partition("::")[0])
        if self._empty is None:
            return self._d[key]
        return self._d.get(key, self._empty)

    def get(self, key: str | None, default: Any = None):
        if key is not None:
            self._t.see(key.partition("::")[0])
        return self._d.get(key, default)

    def __contains__(self, key: str | None) -> bool:
        if key is not None:
            self._t.see(key.partition("::")[0])
        return key in self._d


class _Files:
    """`path in files` is an existence probe -- recorded even when the answer
    is no; `files[path]` is the path's facts."""
    __slots__ = ("_t",)

    def __init__(self, t: Tables):
        self._t = t

    def __contains__(self, path: str) -> bool:
        self._t.probe(path)
        return self._t.source.exists(path)

    def __getitem__(self, path: str) -> FileFacts:
        self._t.probe(path)
        return self._t.source.facts(path)


class Tables:
    def __init__(self, source: Any, index: Callable[[str, FileFacts], None]):
        self.source = source
        self._index = index
        self.defs_d: dict[str, Definition] = {}
        self.def_file_d: dict[str, str] = {}
        self.members_d: dict[str, dict[str, str]] = defaultdict(dict)
        self.bindings_d: dict[str, dict[str, Any]] = defaultdict(dict)
        self.hints_d: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        self.exports_d: dict[str, list] = {}
        self.defs = _View(self, self.defs_d)
        self.def_file = _View(self, self.def_file_d)
        self.members = _View(self, self.members_d, _EMPTY)
        self.bindings = _View(self, self.bindings_d, _EMPTY)
        self.hints = _View(self, self.hints_d, _EMPTY)
        self.exports = _View(self, self.exports_d)
        self.files = _Files(self)
        self._loaded: set[str] = set()
        self.current: str | None = None
        self.deps: set[str] = set()
        self.names: set[str] = set()

    def see(self, path: str) -> None:
        if path not in self._loaded:
            self._loaded.add(path)
            if self.source.exists(path):
                self._index(path, self.source.facts(path))
        if path != self.current:
            self.deps.add(path)

    def probe(self, path: str) -> None:
        if path != self.current:
            self.deps.add(path)

    def looked_up(self, name: str) -> None:
        """The current file asked for the unique-name answer on `name`. Called
        on every lookup, cache hit or not: the answer depends on the name alone,
        so it may be shared across files, but whose answer it is may not."""
        self.names.add(name)

    def begin(self, path: str) -> None:
        """Start resolving `path`: its own reads are not dependencies."""
        self.current = path
        self.deps = set()
        self.names = set()
        self.see(path)
