"""Python call and import resolution, without a type checker (CODE-GRAPH D-6).

Every edge carries the TIER that produced it, strongest first:

  scope        a name defined in an enclosing function or the module
  import       a name bound by an import, followed through re-exports
  self         `self.m()` / `cls.m()` / `super().m()` through first-party bases
  class-attr   `Cls.m()` on a first-party class
  local-type   `x = Cls(...)` earlier in the same function, then `x.m()`
  annotation   a parameter or variable annotated with a first-party class
  attr-type    `self.x = Cls(...)` / `self.x: Cls` somewhere in the class
  name-unique  exactly one first-party definition carries the name, and the
               name is not one a builtin or stdlib object also answers to

What is NOT an edge: a call provably into a library (its receiver is a
library module, class or instance) is dropped; a call the rules cannot place
goes to `unresolved`, which answers carry as candidates. An edge is never
guessed between two candidates -- a missed caller is recoverable from the
candidates, an invented one is believed.
"""
from __future__ import annotations

import builtins
from collections import defaultdict
from dataclasses import dataclass

from .model import CallSite, Definition, FileFacts, ImportBinding
from .stoplist import PY_STOPLIST

TIER_CONF = {"scope": 3, "import": 3, "self": 3, "class-attr": 3,
             "local-type": 2, "annotation": 2, "attr-type": 2, "name-unique": 1}
_BUILTINS = frozenset(dir(builtins))
_MAX_HOPS = 6

EXTERNAL = ("external", None)
UNKNOWN = (None, None)


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    kind: str          # CALLS | IMPORTS | INHERITS
    tier: str
    callee: str | None
    file: str
    line: int


@dataclass(frozen=True)
class Unresolved:
    src: str
    name: str
    receiver: str | None
    file: str
    line: int


def _module_of(path: str) -> str:
    return f"{path}::<module>"


class PyResolver:
    def __init__(self, facts: dict[str, FileFacts], test_paths: set[str] | frozenset[str] = frozenset()):
        self.facts = facts
        self.test_paths = test_paths
        self.defs: dict[str, Definition] = {}
        self.members: dict[str, dict[str, str]] = defaultdict(dict)   # scope -> name -> qname
        self.by_name: dict[str, list[str]] = defaultdict(list)
        self.bindings: dict[str, dict[str, ImportBinding]] = defaultdict(dict)  # scope -> local -> binding
        self.hints: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        self.def_file: dict[str, str] = {}
        for path, f in facts.items():
            for d in f.definitions:
                self.defs[d.qname] = d
                self.def_file[d.qname] = path
                self.members[d.parent or _module_of(path)][d.name] = d.qname
                self.by_name[d.name].append(d.qname)
            for b in f.imports:
                self.bindings[b.scope].setdefault(b.local, b)
            for h in f.hints:
                self.hints[h.scope][h.target].append(h)
        self._mro_cache: dict[str, tuple[list[str], bool]] = {}
        # Memo tables for pure lookups over the tables above (nothing below
        # writes to them once built). `type_of` recurses through every hint
        # of a name, and `x = x.parent` written k times in one function makes
        # that k**_MAX_HOPS calls: 310M calls for 1.3M receivers on the 100k
        # proxy, 220 of 300 profiled seconds. Depth stays in the key, so a
        # result cut short by the hop limit is never reused where the limit
        # would not have cut it -- the output is the uncached one, exactly.
        self._type_cache: dict[tuple, tuple[str | None, str | None, str | None]] = {}
        self._chain_cache: dict[tuple[str, str], tuple[str, ...]] = {}
        self._local_cache: dict[str, bool] = {}
        self._guess_cache: dict[tuple[str, bool], tuple[str | None, str | None, bool]] = {}

    # --- modules ---------------------------------------------------------------

    def _path_for(self, parts: list[str]) -> str | None:
        if not parts:
            return None
        stem = "/".join(parts)
        for cand in (f"{stem}.py", f"{stem}/__init__.py", f"src/{stem}.py", f"src/{stem}/__init__.py"):
            if cand in self.facts:
                return cand
        return None

    def module_path(self, from_path: str, module: str, level: int) -> str | None:
        parts = [p for p in module.split(".") if p]
        if level:
            base = from_path.split("/")[:-1]
            if level > 1:
                base = base[: len(base) - (level - 1)] if level - 1 <= len(base) else []
            return self._path_for(base + parts) if (base or parts) else None
        found = self._path_for(parts)
        here = from_path.split("/")[:-1]
        if found is None and here and "/".join(here + ["__init__.py"]) not in self.facts:
            # A SCRIPT importing its sibling: a script's own directory is on
            # sys.path. A module inside a package gets no such lookup -- there
            # `import platform` is the stdlib, not the sibling package
            # `pkg/platform/` (the oracle caught exactly that).
            found = self._path_for(here + parts)
        return found

    def resolve_binding(self, from_path: str, b: ImportBinding, hops: int = 0) -> tuple[str | None, str | None]:
        """-> ('module', path) | ('symbol', qname) | EXTERNAL | UNKNOWN"""
        if hops > _MAX_HOPS:
            return UNKNOWN
        if b.attr is None:
            path = self.module_path(from_path, b.module, b.level)
            return ("module", path) if path else EXTERNAL
        if b.attr == "*":
            return UNKNOWN
        as_module = self.module_path(from_path, f"{b.module}.{b.attr}" if b.module else b.attr, b.level)
        if as_module:
            return ("module", as_module)
        path = self.module_path(from_path, b.module, b.level)
        if path is None:
            return EXTERNAL
        return self.symbol_in(path, b.attr, hops + 1)

    def imported_module(self, from_path: str, b: ImportBinding) -> str | None:
        """The module an import statement NAMES: `from pkg import Name` depends
        on `pkg` even when `Name` is defined three re-exports away, and
        `from pkg import sub` on `pkg.sub` when that is a module."""
        if b.attr not in (None, "*"):
            sub = self.module_path(from_path, f"{b.module}.{b.attr}" if b.module else b.attr, b.level)
            if sub:
                return sub
        return self.module_path(from_path, b.module, b.level)

    def symbol_in(self, path: str, name: str, hops: int = 0) -> tuple[str | None, str | None]:
        q = self.members[_module_of(path)].get(name)
        if q:
            return ("symbol", q)
        b = self.bindings[_module_of(path)].get(name)
        if b is not None:
            return self.resolve_binding(path, b, hops + 1)
        for star in (x for x in self.bindings[_module_of(path)].values() if x.attr == "*"):
            target = self.module_path(path, star.module, star.level)
            if target and target != path:
                found = self.symbol_in(target, name, hops + 1)
                if found[0] == "symbol":
                    return found
        return UNKNOWN

    # --- scopes and types -----------------------------------------------------

    def _chain(self, scope: str, path: str) -> tuple[str, ...]:
        """Lookup scopes for a name used in `scope`: enclosing FUNCTIONS, then
        the module. Class bodies are skipped, as Python skips them."""
        key = (scope, path)
        hit = self._chain_cache.get(key)
        if hit is not None:
            return hit
        out: list[str] = []
        q: str | None = scope
        while q and q in self.defs:
            if self.defs[q].kind != "class":
                out.append(q)
            q = self.defs[q].parent
        out.append(_module_of(path))
        self._chain_cache[key] = tuple(out)
        return self._chain_cache[key]

    def lookup_name(self, name: str, scope: str, path: str) -> tuple[str | None, str | None, str | None]:
        """-> (kind, target, tier) for a bare name."""
        for s in self._chain(scope, path):
            q = self.members[s].get(name)
            if q:
                return ("symbol", q, "scope")
            b = self.bindings[s].get(name)
            if b is not None:
                kind, target = self.resolve_binding(path, b)
                return (kind, target, "import")
        found = self.symbol_in(path, name)            # star imports
        if found[0] == "symbol":
            return ("symbol", found[1], "import")
        if name in _BUILTINS:
            return ("external", None, None)
        return (None, None, None)

    def type_of(self, expr: str, scope: str, path: str, klass: str | None, depth: int = 0
                ) -> tuple[str | None, str | None, str | None]:
        """-> (kind, target, tier): kind in module | class | instance | external | None."""
        key = (expr, scope, path, klass, depth)
        hit = self._type_cache.get(key)
        if hit is None:
            hit = self._type_cache[key] = self._type_of(expr, scope, path, klass, depth)
        return hit

    def _type_of(self, expr: str, scope: str, path: str, klass: str | None, depth: int
                 ) -> tuple[str | None, str | None, str | None]:
        if depth > _MAX_HOPS or not expr:
            return (None, None, None)
        if expr.startswith("self.") and klass and expr.count(".") == 1:
            for h in self.hints[klass].get(expr, []):
                kind, target, _ = self.type_of(h.type_expr, klass, self.def_file.get(klass, path), klass, depth + 1)
                if kind == "class":
                    return ("instance", target, "attr-type")
                if kind == "external":
                    return ("external", None, None)
            return (None, None, None)
        if "(" in expr or "[" in expr:
            return (None, None, None)
        head, _, rest = expr.partition(".")
        if not rest:
            for s in self._chain(scope, path):
                for h in self.hints[s].get(expr, []):
                    kind, target, _ = self.type_of(h.type_expr, s, path, klass, depth + 1)
                    if kind == "class":
                        return ("instance", target, "local-type" if h.source == "ctor" else "annotation")
                    if kind == "external":
                        return ("external", None, None)
            kind, target, tier = self.lookup_name(expr, scope, path)
            if kind == "symbol" and target in self.defs:
                return ("class" if self.defs[target].kind == "class" else "function", target, tier)
            return (kind, target, tier)
        # a dotted expression: an import bound under its full dotted name first
        for s in self._chain(scope, path):
            b = self.bindings[s].get(expr)
            if b is not None:
                kind, target = self.resolve_binding(path, b)
                return (kind, target, "import")
        kind, target, tier = self.type_of(head, scope, path, klass, depth + 1)
        for part in rest.split("."):
            if kind == "module":
                # only a package has submodules; `a/b.py` + `.c` is not `a/c.py`
                sub = (self.module_path(target, part, 1)
                       if target and target.endswith("__init__.py") else None)
                if sub and sub != target:
                    kind, target = "module", sub
                    continue
                found = self.symbol_in(target, part)
                if found[0] == "symbol":
                    kind = "class" if self.defs[found[1]].kind == "class" else "function"
                    target = found[1]
                    continue
                kind, target = found
                if kind == "module":
                    continue
                return (kind, target, tier)
            if kind == "external":
                return ("external", None, None)
            return (None, None, None)
        return (kind, target, tier)

    def mro(self, klass: str) -> tuple[list[str], bool]:
        """(first-party classes in lookup order, has a base outside our code)."""
        if klass in self._mro_cache:
            return self._mro_cache[klass]
        self._mro_cache[klass] = ([klass], False)            # cycle guard
        order, external = [klass], False
        d = self.defs[klass]
        path = self.def_file[klass]
        scope = d.parent or _module_of(path)
        for base in d.bases:
            kind, target, _ = self.type_of(base, scope, path, None)
            if kind == "class" and target:
                sub, ext = self.mro(target)
                order += [c for c in sub if c not in order]
                external = external or ext
            elif base not in ("object",):
                external = True
        self._mro_cache[klass] = (order, external)
        return order, external

    def member(self, klass: str, name: str, skip_self: bool = False) -> tuple[str | None, bool]:
        order, external = self.mro(klass)
        for c in order[1:] if skip_self else order:
            q = self.members[c].get(name)
            if q:
                return q, external
        return None, external

    # --- calls --------------------------------------------------------------

    def resolve_call(self, c: CallSite, path: str) -> tuple[str | None, str | None, bool]:
        """-> (target qname, tier, is_external)."""
        if c.receiver is None:
            kind, target, tier = self.lookup_name(c.callee, c.caller, path)
            if kind == "symbol" and target:
                return target, tier, False
            if kind in ("external", "module"):
                return None, None, kind == "external"
            return self._guess(c.callee, path)
        if c.receiver in ("self", "cls") and c.klass in self.defs:
            q, external = self.member(c.klass, c.callee)
            if q:
                return q, "self", False
            return None, None, external
        if c.receiver == "super()" and c.klass in self.defs:
            q, external = self.member(c.klass, c.callee, skip_self=True)
            if q:
                return q, "self", False
            return None, None, external
        kind, target, tier = self.type_of(c.receiver, c.caller, path, c.klass)
        if kind == "module" and target:
            found = self.symbol_in(target, c.callee)
            if found[0] == "symbol":
                return found[1], "import", False
            return (None, None, True) if found == EXTERNAL else self._guess(c.callee, path)
        if kind in ("class", "instance") and target in self.defs:
            q, external = self.member(target, c.callee)
            if q:
                return q, ("class-attr" if kind == "class" else tier), False
            if external:
                return None, None, True
            return None, None, False
        if kind == "external":
            return None, None, True
        return self._guess(c.callee, path)

    def _is_local(self, qname: str) -> bool:
        """Defined inside a function (at any depth): unreachable by name from
        anywhere else, so never a candidate for a guess."""
        hit = self._local_cache.get(qname)
        if hit is not None:
            return hit
        local = False
        q = self.defs[qname].parent
        while q and q in self.defs:
            if self.defs[q].kind != "class":
                local = True
                break
            q = self.defs[q].parent
        self._local_cache[qname] = local
        return local

    def _guess(self, name: str, caller_path: str) -> tuple[str | None, str | None, bool]:
        if name in PY_STOPLIST or name in _BUILTINS:
            return None, None, False
        from_test = caller_path in self.test_paths
        key = (name, from_test)
        hit = self._guess_cache.get(key)
        if hit is not None:
            return hit
        hits = [q for q in self.by_name.get(name, [])
                if not self._is_local(q) and (from_test or self.def_file[q] not in self.test_paths)]
        self._guess_cache[key] = (hits[0], "name-unique", False) if len(hits) == 1 else (None, None, False)
        return self._guess_cache[key]

    # --- everything ---------------------------------------------------------

    def resolve(self) -> tuple[list[Edge], list[Unresolved]]:
        edges: list[Edge] = []
        unresolved: list[Unresolved] = []
        for path, f in self.facts.items():
            self._type_cache.clear()       # its keys carry the file: bounded memory, little lost
            seen_imports: set[str] = set()
            for b in f.imports:
                dst_path = self.imported_module(path, b)
                if dst_path and dst_path != path and dst_path not in seen_imports:
                    seen_imports.add(dst_path)
                    edges.append(Edge(_module_of(path), _module_of(dst_path), "IMPORTS", "import", None, path, 0))
            for d in f.definitions:
                if d.kind != "class":
                    continue
                scope = d.parent or _module_of(path)
                for base in d.bases:
                    kind, target, tier = self.type_of(base, scope, path, None)
                    if kind == "class" and target and target != d.qname:
                        edges.append(Edge(d.qname, target, "INHERITS", tier or "scope", None,
                                          path, d.spans[0][0]))
            for c in f.calls:
                target, tier, external = self.resolve_call(c, path)
                if target:
                    edges.append(Edge(c.caller, target, "CALLS", tier, c.callee, path, c.line))
                elif not external:
                    unresolved.append(Unresolved(c.caller, c.callee, c.receiver, path, c.line))
        return edges, unresolved
