"""Java and Kotlin resolution without a compiler (CODE-GRAPH D3).

One resolver for both languages: they share one namespace of fully qualified
names and call each other inside one module, so a Kotlin call into a Java
class and a Java call into a Kotlin companion are ordinary edges here.

A type is found by its fully qualified name -- `<package>.<Name>` -- never by
the file's path: the package index (`files.package`) lists the files of a
package, and a file's top-level definitions are looked up in them. Listing a
package is recorded as a read of the pseudo-path `jvm-package:<name>`, so a
file that enters or leaves the package re-resolves every file that looked
(see update).

A simple name means, in order: a local or parameter of an enclosing function;
a member of an implicit receiver (an extension function's receiver, the
enclosing classes with everything they inherit, their companions); a
definition of the file; an explicit import; a definition of the file's
package; a wildcard import; the platform (`java.lang`, Kotlin's default
imports -- external).

Tiers, strongest first: `scope` (a definition of the file or an enclosing
function), `self` (a member through `this`, said or implied), `import`
(through an import, the package or a fully qualified name), `class-attr` (a
static member: a class's, a companion's, an object's), `annotation`,
`local-type`, `attr-type` (a member of a declared, constructed or field
type), `extension` (a Kotlin extension function whose receiver type matches),
and `variant`: one name defined in several build-flavor source sets, an edge
to each definition the caller's source set can see -- a guess about which one
the build picks, named as such (decision D3).

There is no unique-name tier. A call the rules cannot place is `unresolved`;
one provably into the platform or a library draws nothing.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from . import languages
from .model import RETURN, CallSite, FileFacts
from .resolve_py import Edge, Unresolved
from .stoplist import JVM_ANY_MEMBERS, JVM_GLOBALS
from .tables import MemorySource, Tables

CONTAINERS = ("class", "interface", "enum", "object")
# The unique-name tier does not exist here; types are never a method call's target anyway.
NEVER_GUESSED = ("class", "interface", "enum", "object")
_MAX_HOPS = 8
_TEST_SETS = ("androidTest", "testFixtures", "test")
_EVERY_FLAVOR = frozenset({"", "main", "debug", "release"})
_CAST = re.compile(r"^\((.+?)\s+as\??\s+(.+)\)$")

# A resolved meaning: (kind, targets, tier, simple type name). kind is
# instance | class (static access to a type) | facade (a Kotlin file seen from
# Java; targets are paths) | function | method | external | None (unknown).
Meaning = tuple[str | None, tuple[str, ...], str | None, str | None]
_UNKNOWN: Meaning = (None, (), None, None)


def _module(path: str) -> str:
    return f"{path}::<module>"


def _file(qname: str) -> str:
    return qname.partition("::")[0]


def _ext(name: str | None = None) -> Meaning:
    return ("external", (), None, name)


def source_set(path: str) -> str | None:
    """`app/src/gplay/java/...` -> `gplay`; None outside a `src/<set>/` layout."""
    parts = path.split("/")
    for i, part in enumerate(parts[:-2]):
        if part == "src":
            return parts[i + 1]
    return None


def _flavor(source: str | None) -> str:
    """The flavor a source set is compiled with; '' when it is compiled with
    every flavor (main, the build types, the plain test sets)."""
    s = source or ""
    for prefix in _TEST_SETS:
        if s.startswith(prefix):
            s = s[len(prefix):]
            s = s[:1].lower() + s[1:]
            break
    return "" if s in _EVERY_FLAVOR else s


def _sees(flavor: str, theirs: str | None) -> bool:
    if not flavor or theirs is None or theirs in _EVERY_FLAVOR or theirs.startswith(_TEST_SETS):
        return True
    a, b = flavor.lower(), theirs.lower()
    return a.startswith(b) or b.startswith(a)


def _type_name(expr: str) -> str | None:
    """A type as written -> the dotted class name it names, or None for what
    has no members to call (arrays, function types, primitives are external
    by name). `@NonNull java.util.List<String>?` -> `java.util.List`."""
    t = re.sub(r"@\w+(\([^)]*\))?\s*", "", expr).strip()
    t = re.sub(r"^(out|in|vararg|final)\s+", "", t)
    if not t or "->" in t or t.endswith("[]") or t.startswith("("):
        return None
    depth, cut = 0, []
    for ch in t:                                   # drop generic arguments at every depth
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif depth == 0:
            cut.append(ch)
    t = "".join(cut).rstrip("?! ").strip()
    return t if re.fullmatch(r"[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)*", t) else None


def _segments(expr: str) -> list[str] | None:
    """`a.b(x, y.z()).c` -> ['a', 'b(...)', 'c']: the expression's steps at the
    top level, a call's arguments folded away. None for what is not a chain
    of names and calls (an index, a lambda, a string template)."""
    out, cur, depth = [], [], 0
    i = 0
    while i < len(expr):
        ch = expr[i]
        if ch in "([{":
            if ch != "(" and depth == 0:              # an index or a lambda: not a chain of names
                return None
            if depth == 0:
                cur.append("(...)")
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                return None
        elif depth == 0:
            if ch == ".":
                out.append("".join(cur).strip())
                cur = []
            else:
                cur.append(ch)
        i += 1
    if depth:
        return None
    out.append("".join(cur).strip())
    if any(not s or " " in s.replace("new ", "", 1) for s in out):
        return None
    return out


class JvmResolver:
    """As the other resolvers: `facts` + `test_paths` in memory (the full
    build), or `source` reading the stored graph (an update); either way the
    tables fill one file at a time and record what each file read."""

    def __init__(self, facts: dict[str, FileFacts] | None = None,
                 test_paths: set[str] | frozenset[str] = frozenset(), source: Any = None):
        self.source = source if source is not None else MemorySource(facts or {}, test_paths, CONTAINERS)
        self.t = Tables(self.source, self._index)
        self.facts, self.defs, self.def_file = self.t.files, self.t.defs, self.t.def_file
        self.members, self.hints, self.exports = self.t.members, self.t.hints, self.t.exports
        self.file_deps: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
        # Per-file memo tables (cleared for every resolved file: a hit must be
        # this file's own, recorded read -- see tables).
        self._packages: dict[str, tuple[str, ...]] = {}
        self._lineage: dict[str, tuple[list[str], bool]] = {}
        self._types: dict[tuple, Meaning] = {}
        self._fqn: dict[str, tuple[str, tuple[str, ...]]] = {}

    def _index(self, path: str, f: FileFacts) -> None:
        t = self.t
        for d in f.definitions:
            t.defs_d[d.qname] = d
            t.def_file_d[d.qname] = path
            t.members_d[d.parent or _module(path)].setdefault(d.name, d.qname)
        t.exports_d[path] = list(f.exports)
        for h in f.hints:
            t.hints_d[h.scope][h.target].append(h)

    # --- the namespace ----------------------------------------------------------

    def package_files(self, package: str) -> tuple[str, ...]:
        if package not in self._packages:
            self.t.probe(languages.PACKAGE_DEP + package)
            self._packages[package] = tuple(sorted(self.source.package_files(package)))
        return self._packages[package]

    def _top_level(self, package: str, name: str) -> tuple[str, ...]:
        """The definitions called `name` at the top of the package's files --
        several when build flavors each define it."""
        return tuple(q for p in self.package_files(package) if (q := self.members[_module(p)].get(name)))

    def _facades(self, package: str, name: str) -> tuple[str, ...]:
        """Kotlin files of the package that Java sees as the class `name`."""
        return tuple(p for p in self.package_files(package)
                     if any(e.local == name for e in self.exports.get(p) or ()))

    def fqn(self, dotted: str) -> tuple[str, tuple[str, ...]]:
        """('symbol', qnames) | ('facade', paths) | ('', ()) for a fully
        qualified name: `a.b.C`, a nested `a.b.C.D`, a Kotlin top-level
        `a.b.fn`, a facade `a.b.FooKt` or a member of one `a.b.FooKt.fn`."""
        if dotted in self._fqn:
            return self._fqn[dotted]
        parts = [p for p in dotted.split(".") if p]
        found: tuple[str, tuple[str, ...]] = ("", ())
        for i in range(len(parts) - 1, -1, -1):
            package, rest = ".".join(parts[:i]), parts[i:]
            tops = self._top_level(package, rest[0])
            for name in rest[1:]:
                tops = tuple(q for t in tops if (q := self.members[t].get(name)))
            if tops:
                found = ("symbol", tops)
                break
            facades = self._facades(package, rest[0])
            if facades and len(rest) == 1:
                found = ("facade", facades)
                break
            if facades and len(rest) == 2:
                members = tuple(q for p in facades if (q := self.members[_module(p)].get(rest[1])))
                if members:
                    found = ("symbol", members)
                    break
        self._fqn[dotted] = found
        return found

    def _meaning_of(self, qnames: tuple[str, ...], tier: str | None) -> Meaning:
        d = self.defs[qnames[0]]
        if d.kind in CONTAINERS:
            return ("class", qnames, tier, d.name)
        return (d.kind, qnames, tier, None)

    # --- scopes, lineage --------------------------------------------------------

    def _enclosing(self, scope: str) -> tuple[list[str], list[str]]:
        """(enclosing functions, enclosing types), innermost first."""
        fns, types = [], []
        q: str | None = scope
        while q and q in self.defs:
            (types if self.defs[q].kind in CONTAINERS else fns).append(q)
            q = self.defs[q].parent
        return fns, types

    def lineage(self, klass: str) -> tuple[list[str], bool]:
        """The type and its first-party supertypes, nearest first; True when a
        supertype is outside the graph (an enum's is: java.lang.Enum)."""
        if klass in self._lineage:
            return self._lineage[klass]
        order: list[str] = []
        external, todo = False, [klass]
        while todo:
            c = todo.pop(0)
            if c in order or c not in self.defs:
                continue
            order.append(c)
            d = self.defs[c]
            if d.kind == "enum":
                external = True
            for base in d.bases:
                kind, targets, _, _ = self.type_of_type(base, d.parent or _module(_file(c)), _file(c))
                if kind == "instance":
                    todo.extend(targets)
                else:
                    external = True
        self._lineage[klass] = (order, external)
        return self._lineage[klass]

    def _find_member(self, types: Iterable[str], name: str) -> tuple[tuple[str, ...], bool]:
        """The nearest definition of `name` in each type's lineage; True when a
        lineage reaches outside the graph."""
        found: list[str] = []
        external = False
        for k in types:
            order, ext = self.lineage(k)
            external |= ext
            for c in order:
                q = self.members[c].get(name)
                if q:
                    found.append(q)
                    break
        return tuple(dict.fromkeys(found)), external

    def _companions(self, klass: str) -> list[str]:
        return [q for q in self.members[klass].values()
                if q in self.defs and self.defs[q].kind == "object" and "companion object" in self.defs[q].signature]

    def _static_member(self, types: tuple[str, ...], name: str) -> tuple[str, ...]:
        """`Foo.name` on the type: its own or inherited member, else its companion's."""
        found, _ = self._find_member(types, name)
        if found:
            return found
        return tuple(dict.fromkeys(q for k in types for comp in self._companions(k)
                                   for q in self._find_member((comp,), name)[0]))

    def _property(self, types: tuple[str, ...], name: str, depth: int) -> Meaning:
        """The type of `x.name` for x of one of `types`: a field or property
        hint in the lineage, an enum constant, a companion's property -- or,
        Kotlin reading a Java bean, what `getName()` / `isName()` returns."""
        for k in types:
            order, _ = self.lineage(k)
            for c in order + [q for t in order for q in self._companions(t)]:
                for h in self.hints[c].get(f"this.{name}", ()):
                    m = self._hint_type(h, c, _file(c), depth + 1)
                    if m[0]:
                        return (m[0], m[1], "attr-type", m[3])
        if name[:1].islower():
            cap = name[:1].upper() + name[1:]
            for getter in (f"get{cap}", f"is{cap}"):
                found, _ = self._find_member(types, getter)
                if found and self.defs[found[0]].kind == "method":
                    m = self._result(found, depth + 1)
                    if m[0]:
                        return (m[0], m[1], "attr-type", m[3])
        return _UNKNOWN

    # --- names ------------------------------------------------------------------

    def _imports(self, path: str):
        return self.facts[path].imports

    def _package_of(self, path: str) -> str:
        return self.facts[path].package or ""

    def _global(self, name: str, path: str, types_only: bool = False) -> Meaning:
        """What `name` means at the top of the file: the file's own
        definitions, its imports, its package, its wildcards, the platform."""
        q = self.members[_module(path)].get(name)
        if q and (not types_only or self.defs[q].kind in CONTAINERS):
            return self._meaning_of((q,), "scope")
        for b in self._imports(path):
            if b.attr == "*" or b.local != name:
                continue
            kind, targets = self.fqn(f"{b.module}.{b.attr}" if b.module else b.attr or "")
            if kind == "facade":
                return ("facade", targets, "import", name)
            if kind == "symbol":
                return self._meaning_of(targets, "import")
            # Nothing first-party by that name. External -- unless the import
            # points into a first-party package: then it names what the graph
            # does not hold (a Kotlin property, a file outside the universe),
            # and a call through it is unresolved, never silently external.
            parts = b.module.split(".")
            if any(self.package_files(".".join(parts[:i])) for i in range(len(parts), 0, -1)):
                return _UNKNOWN
            return _ext(name)
        package = self._package_of(path)
        tops = self._top_level(package, name)
        if tops:
            return self._meaning_of(tops, "import")
        facades = self._facades(package, name)
        if facades:
            return ("facade", facades, "import", name)
        for b in self._imports(path):
            if b.attr != "*":
                continue
            if b.level:                              # `import static a.b.C.*`
                kind, types = self.fqn(b.module)
                if kind == "symbol":
                    found = self._static_member(types, name)
                    if found:
                        return self._meaning_of(found, "import")
                elif kind == "facade":
                    found = tuple(q for p in types if (q := self.members[_module(p)].get(name)))
                    if found:
                        return self._meaning_of(found, "import")
                continue
            tops = self._top_level(b.module, name)
            if tops:
                return self._meaning_of(tops, "import")
            facades = self._facades(b.module, name)
            if facades:
                return ("facade", facades, "import", name)
        if name in JVM_GLOBALS:
            return _ext(name)
        return _UNKNOWN

    def lookup(self, name: str, scope: str, path: str, depth: int) -> Meaning:
        """What a simple name used in `scope` means (a value or a type)."""
        fns, types = self._enclosing(scope)
        for s in fns:
            for h in self.hints[s].get(name, ()):
                m = self._hint_type(h, s, path, depth + 1)
                if m[0]:
                    return m
            q = self.members[s].get(name)
            if q:
                return self._meaning_of((q,), "scope")
        for s in fns:                                # the receiver of an enclosing extension function
            for h in self.hints[s].get("this", ()):
                recv = self._hint_type(h, s, path, depth + 1)
                if recv[0] == "instance":
                    m = self._property(recv[1], name, depth)
                    if m[0]:
                        return m
        for k in types:
            order, _ = self.lineage(k)
            for c in order + [q for t in order for q in self._companions(t)]:
                for target in (f"this.{name}", name):
                    for h in self.hints[c].get(target, ()):
                        m = self._hint_type(h, c, _file(c), depth + 1)
                        if m[0]:
                            return (m[0], m[1], "attr-type", m[3])
                q = self.members[c].get(name)
                if q:
                    return self._meaning_of((q,), "self")
            m = self._property((k,), name, depth)     # an inherited Java getter read as a property
            if m[0]:
                return m
        for h in self.hints[_module(path)].get(name, ()):       # a top-level property
            m = self._hint_type(h, _module(path), path, depth + 1)
            if m[0]:
                return m
        return self._global(name, path)

    # --- types ------------------------------------------------------------------

    def type_of_type(self, expr: str, scope: str, path: str) -> Meaning:
        """A TYPE as written -> ('instance', classes) | external | unknown."""
        key = ("type", expr, scope, path)
        if key in self._types:
            return self._types[key]
        self._types[key] = _UNKNOWN                  # a type naming itself through its bases
        name = _type_name(expr)
        found: Meaning = _UNKNOWN
        if name:
            head, _, rest = name.partition(".")
            m = self._type_lookup(head, scope, path)
            for part in rest.split(".") if rest and m[0] == "class" else []:
                nested = tuple(q for t in m[1] if (q := self.members[t].get(part))
                               and self.defs[q].kind in CONTAINERS)
                m = self._meaning_of(nested, "import") if nested else _UNKNOWN
            if m[0] != "class" and rest:
                kind, targets = self.fqn(name)
                if kind == "symbol" and self.defs[targets[0]].kind in CONTAINERS:
                    m = self._meaning_of(targets, "import")
            if m[0] == "class":
                found = ("instance", m[1], "annotation", m[3])
            elif m[0] == "external" or (m[0] is None and (head in JVM_GLOBALS or head[:1].islower())):
                found = _ext(name.rsplit(".", 1)[-1])
        self._types[key] = found
        return found

    def _type_lookup(self, name: str, scope: str, path: str) -> Meaning:
        """A simple TYPE name: nested types of the enclosing types (inherited
        ones too), then the file, imports, package, wildcards, the platform."""
        _fns, types = self._enclosing(scope)
        for s in _fns:
            q = self.members[s].get(name)
            if q and self.defs[q].kind in CONTAINERS:
                return self._meaning_of((q,), "scope")
        for k in types:
            if self.defs[k].name == name:
                return self._meaning_of((k,), "scope")
            order, _ = self.lineage(k)
            for c in order:
                q = self.members[c].get(name)
                if q and self.defs[q].kind in CONTAINERS:
                    return self._meaning_of((q,), "scope")
        return self._global(name, path, types_only=True)

    def _hint_type(self, h, scope: str, path: str, depth: int) -> Meaning:
        if depth > _MAX_HOPS:
            return _UNKNOWN
        origin = h.origin or scope
        opath = _file(origin) if "::" in origin else path
        if h.source == "annotation":
            return self.type_of_type(h.type_expr, origin, opath)
        if h.source == "ctor":
            m = self.type_of_type(h.type_expr, origin, opath)
            return ("instance", m[1], "local-type", m[3]) if m[0] == "instance" else m
        if h.source == "call":
            return self._call_value(h.type_expr, origin, opath, depth + 1)
        if h.source == "alias":
            return self.type_of_expr(h.type_expr, origin, opath, depth + 1)
        return _UNKNOWN

    def _result(self, targets: tuple[str, ...], depth: int) -> Meaning:
        """The value a call of `targets` returns: an instance for a
        constructor, else the declared (or, in Kotlin, inferred-from-body)
        return type of the first target."""
        d = self.defs[targets[0]]
        if d.kind in CONTAINERS:
            return ("instance", tuple(t for t in targets if self.defs[t].kind in CONTAINERS), "local-type", d.name)
        where = d.parent or _module(_file(d.qname))
        if d.returns:
            m = self.type_of_type(d.returns, where, _file(d.qname))
            return (m[0], m[1], "local-type", m[3]) if m[0] == "instance" else m
        for h in self.hints[d.qname].get(RETURN, ()):
            m = self._hint_type(h, d.qname, _file(d.qname), depth + 1)
            if m[0]:
                return (m[0], m[1], "local-type", m[3])
        return _UNKNOWN

    def _call_value(self, callee: str, scope: str, path: str, depth: int) -> Meaning:
        """The value of `callee(...)`: `Foo` constructs, `Helper.create` returns."""
        receiver, _, name = callee.rpartition(".")
        if receiver:
            targets, _tier, external = self._call_on(self.type_of_expr(receiver, scope, path, depth + 1),
                                                     name, scope, path, depth)
        else:
            targets, _tier, external = self._bare(name, scope, path, depth)
        if targets:
            return self._result(targets, depth)
        return _ext() if external else _UNKNOWN

    def type_of_expr(self, expr: str, scope: str, path: str, depth: int = 0) -> Meaning:
        key = ("expr", expr, scope, path, depth)
        if key not in self._types:
            self._types[key] = _UNKNOWN
            self._types[key] = self._type_of_expr(expr, scope, path, depth)
        return self._types[key]

    def _type_of_expr(self, expr: str, scope: str, path: str, depth: int) -> Meaning:
        if depth > _MAX_HOPS or not expr:
            return _UNKNOWN
        expr = expr.strip()
        cast = _CAST.match(expr)
        if cast:                                      # `(x as Repo)`
            return self.type_of_type(cast.group(2), scope, path)
        if expr[:1] in "\"'":
            return _ext("String")
        steps = _segments(expr)
        if not steps:
            return _UNKNOWN
        head = steps[0]
        fns, types = self._enclosing(scope)
        if head == "this":
            m = _UNKNOWN
            for s in fns:
                for h in self.hints[s].get("this", ()):
                    m = self._hint_type(h, s, path, depth + 1)
                    if m[0]:
                        break
                if m[0]:
                    break
            if not m[0] and types:
                m = ("instance", (types[0],), "self", self.defs[types[0]].name)
        elif head.startswith("new "):
            m = self.type_of_type(head[4:].replace("(...)", ""), scope, path)
            m = ("instance", m[1], "local-type", m[3]) if m[0] == "instance" else m
        elif head.endswith("(...)"):
            m = self._call_value(re.sub(r"<[^>]*>", "", head[:-5]), scope, path, depth)
        else:
            m = self.lookup(head, scope, path, depth)
            if not m[0] and len(steps) > 1:           # a fully qualified name: `com.x.Util.go()`
                for i in range(len(steps), 1, -1):
                    if any("(" in s for s in steps[:i]):
                        continue
                    kind, targets = self.fqn(".".join(steps[:i]))
                    if kind == "symbol":
                        m = self._meaning_of(targets, "import")
                    elif kind == "facade":
                        m = ("facade", targets, "import", steps[i - 1])
                    if m[0]:
                        steps = [steps[i - 1]] + steps[i:]
                        break
        for step in steps[1:]:
            if not m[0] or m[0] == "external":
                break
            if step.endswith("(...)"):
                name = re.sub(r"<[^>]*>", "", step[:-5])           # `<T>emptyList(...)`
                targets, _tier, external = self._call_on(m, name, scope, path, depth)
                m = self._result(targets, depth) if targets else (_ext() if external else _UNKNOWN)
            else:
                m = self._member_value(m, step, depth)
        return m

    def _member_value(self, m: Meaning, name: str, depth: int) -> Meaning:
        """`x.name` (not called) for x meaning `m`."""
        kind, targets = m[0], m[1]
        if kind in ("instance", "class"):
            if name == "INSTANCE" and all(self.defs[t].kind == "object" for t in targets):
                return ("instance", targets, "class-attr", m[3])
            if name == "this" and kind == "class":
                return ("instance", targets, "self", m[3])
            if kind == "class":
                nested = tuple(q for t in targets if (q := self.members[t].get(name))
                               and self.defs[q].kind in CONTAINERS)
                if nested:
                    return self._meaning_of(nested, "class-attr")
            p = self._property(targets, name, depth)
            if p[0]:
                return p
            _, external = self._find_member(targets, name)
            return _ext() if external else _UNKNOWN
        if kind == "facade":
            for p in targets:
                for h in self.hints[_module(p)].get(name, ()):
                    r = self._hint_type(h, _module(p), p, depth + 1)
                    if r[0]:
                        return r
        return _ext() if kind == "external" else _UNKNOWN

    # --- calls ------------------------------------------------------------------

    def _extension(self, name: str, receiver_names: set[str], scope: str, path: str) -> tuple[str, ...]:
        """Kotlin extension functions called `name` visible where the call is
        -- members of the enclosing types (a member extension), the file, its
        imports and package -- whose receiver type is one of `receiver_names`."""
        return tuple(c for c, recv in self._extensions(name, scope, path)
                     if recv.rsplit(".", 1)[-1] in receiver_names)

    def _extensions(self, name: str, scope: str, path: str) -> list[tuple[str, str]]:
        """(qname, receiver type) of every extension function called `name`
        visible where the call is."""
        cands: list[str] = []
        for k in self._enclosing(scope)[1]:
            cands += [q for c in self.lineage(k)[0] if (q := self.members[c].get(name))]
        q = self.members[_module(path)].get(name)
        if q:
            cands.append(q)
        for b in self._imports(path):
            if b.attr != "*" and b.local == name and b.module:
                kind, targets = self.fqn(f"{b.module}.{b.attr}")
                if kind == "symbol":
                    cands += targets
            elif b.attr == "*" and not b.level:
                cands += self._top_level(b.module, name)
        cands += self._top_level(self._package_of(path), name)
        out = []
        for c in dict.fromkeys(cands):
            d = self.defs[c]
            recv = _type_name(d.bases[0]) if d.bases and d.kind in ("function", "method") else None
            if recv:
                out.append((c, recv))
        return out

    def _extension_of_unknown(self, name: str, receiver: str, scope: str, path: str) -> tuple[str, ...]:
        """An extension call on a receiver of unknown type (`it`, a Kotlin read
        of an external getter): the one extension of that name in reach, or
        -- several in reach -- the one whose receiver type the receiver is
        named after (`intent.x()` -> the extension on Intent). A name every
        receiver has is never matched this way."""
        if name in JVM_ANY_MEMBERS:
            return ()
        found = self._extensions(name, scope, path)
        if len(found) == 1:
            return (found[0][0],)
        last = re.split(r"[.(]", receiver)[-1] if receiver else ""
        named = [c for c, recv in found if last and recv.rsplit(".", 1)[-1].lower() == last.lower()]
        return (named[0],) if len(named) == 1 else ()

    def _type_names(self, types: Iterable[str]) -> set[str]:
        """The simple names a value of `types` answers to: each first-party
        type of the lineage AND each supertype written outside the graph
        (`AppCompatActivity` of an Activity) -- what an extension's receiver
        is matched against -- and `Any`."""
        names = {"Any"}
        for t in types:
            for c in self.lineage(t)[0]:
                names.add(self.defs[c].name)
                names |= {n.rsplit(".", 1)[-1] for b in self.defs[c].bases if (n := _type_name(b))}
        return names

    def _receiver_names(self, m: Meaning) -> set[str]:
        if m[0] in ("instance", "class"):
            return self._type_names(m[1])
        if m[0] == "external" and m[3]:
            return {m[3], "Any"}
        return set()

    def _implicit_names(self, scope: str, path: str, depth: int) -> set[str]:
        """What `this` answers to where a bare call is written: an enclosing
        extension function's receiver, then the enclosing types."""
        fns, types = self._enclosing(scope)
        names: set[str] = set()
        for s in fns:
            for h in self.hints[s].get("this", ()):
                recv = self._hint_type(h, s, path, depth + 1)
                names |= self._receiver_names(recv) or ({n.rsplit(".", 1)[-1]} if (n := _type_name(h.type_expr))
                                                        else set())
        return names | self._type_names(types)

    def _call_on(self, m: Meaning, name: str, scope: str, path: str, depth: int, receiver: str = ""
                 ) -> tuple[tuple[str, ...], str | None, bool]:
        """(targets, tier, external) of `x.name(...)` for x meaning `m`;
        `receiver` is x as written, when it is known."""
        kind, targets, tier = m[0], m[1], m[2]
        if kind is None and receiver:
            ext = self._extension_of_unknown(name, receiver, scope, path)
            return (ext, "extension", False) if ext else ((), None, False)
        if kind == "facade":
            found = tuple(q for p in targets if (q := self.members[_module(p)].get(name)))
            return (found, "import", False) if found else ((), None, False)
        if kind in ("instance", "class"):
            found = self._static_member(targets, name) if kind == "class" else self._find_member(targets, name)[0]
            if found:
                return found, ("class-attr" if kind == "class" else (tier or "annotation")), False
            ext = self._extension(name, self._receiver_names(m), scope, path)
            if ext:
                return ext, "extension", False
            _, external = self._find_member(targets, name)
            return (), None, external or name in JVM_ANY_MEMBERS
        if kind == "external":
            # a library type: an extension on it by its name -- or, the name
            # lost on the way (`event.intent.extras`), as for an unknown receiver
            ext = (self._extension(name, self._receiver_names(m), scope, path) if m[3]
                   else self._extension_of_unknown(name, receiver, scope, path) if receiver else ())
            return (ext, "extension", False) if ext else ((), None, True)
        return (), None, False

    def _bare(self, name: str, scope: str, path: str, depth: int, implicit: str | None = None
              ) -> tuple[tuple[str, ...], str | None, bool]:
        """(targets, tier, external) of `name(...)`. `implicit`: the receiver of
        an enclosing Kotlin receiver lambda (`x.apply { name() }`), looked in
        first. Its type unknown or external, a call that would land on the
        very function it is written in is the receiver's own member instead
        -- `builder.run { setProgress(..) }` inside `fun setProgress` -- and
        drawn as nothing."""
        if implicit:
            recv = self.type_of_expr(implicit, scope, path, depth + 1)
            if recv[0] in ("instance", "class"):
                found = (self._static_member(recv[1], name) if recv[0] == "class"
                         else self._find_member(recv[1], name)[0])
                if found:
                    return found, "self", False
                ext = self._extension(name, self._receiver_names(recv), scope, path)
                if ext:
                    return ext, "extension", False
            else:
                targets, tier, external = self._bare(name, scope, path, depth)
                if targets == (scope,):
                    return (), None, True
                return targets, tier, external
        fns, types = self._enclosing(scope)
        for s in fns:
            q = self.members[s].get(name)
            if q:
                return (q,), "scope", False
        for s in fns:                                # an extension function's implicit receiver
            for h in self.hints[s].get("this", ()):
                recv = self._hint_type(h, s, path, depth + 1)
                if recv[0] == "instance":
                    found, _ = self._find_member(recv[1], name)
                    if found:
                        return found, "self", False
        external = False
        for k in types:
            found, ext = self._find_member((k,), name)
            if found:
                return found, "self", False
            external |= ext
            comps = tuple(q for t in self.lineage(k)[0] for comp in self._companions(t)
                          for q in self._find_member((comp,), name)[0])
            if comps:
                return tuple(dict.fromkeys(comps)), "self", False
            if self.defs[k].name == name:            # `Foo()` inside Foo
                return (k,), "scope", False
        m = self._global(name, path)
        if m[0] in ("class", "function", "method") and m[1]:
            targets = m[1]
            if len(targets) > 1:
                # Same-named top-level functions of one package are overloads,
                # extensions of different types among them: the implicit
                # receiver picks the extension it can be (flavor variants all
                # fit it, or none does, and stay as they are).
                names = self._implicit_names(scope, path, depth)
                fit = tuple(t for t in targets if not self.defs[t].bases or self.defs[t].kind not in (
                    "function", "method") or (_type_name(self.defs[t].bases[0]) or "").rsplit(".", 1)[-1] in names)
                targets = fit or targets
            return targets, m[2], False
        if m[0] == "external" or name in JVM_GLOBALS:
            return (), None, True
        return (), None, external or name in JVM_ANY_MEMBERS

    def resolve_call(self, c: CallSite, path: str) -> tuple[tuple[str, ...], str | None, bool]:
        if c.receiver is None:
            return self._bare(c.callee, c.caller, path, 0, c.implicit)
        if c.receiver == "this" and c.klass is None:
            return (), None, False                   # an anonymous class's own method
        if c.receiver == "super":
            _fns, types = self._enclosing(c.caller)
            if not types:
                return (), None, False
            order, external = self.lineage(types[0])
            for ancestor in order[1:]:               # the nearest declaration above the class, alone
                q = self.members[ancestor].get(c.callee)
                if q:
                    return (q,), "self", False
            return (), None, external
        return self._call_on(self.type_of_expr(c.receiver, c.caller, path), c.callee, c.caller, path, 0,
                             receiver=c.receiver)

    # --- the build's entry point ----------------------------------------------

    def _visible(self, caller_path: str, targets: tuple[str, ...]) -> tuple[str, ...]:
        """Of several definitions of one name, the ones the caller's source set
        compiles with; production code never sees a test's. Several left in
        ONE source set are not build variants but an ambiguity the rules
        could not settle (overloads by signature across files): nothing is
        drawn -- a variant edge is one definition per source set."""
        if len(targets) < 2:
            return targets
        flavor = _flavor(source_set(caller_path))
        kept = tuple(t for t in targets if _sees(flavor, source_set(_file(t))))
        if not self.source.is_test(caller_path):
            kept = tuple(t for t in kept if not self.source.is_test(_file(t))) or kept
        kept = kept or targets
        sets = [source_set(_file(t)) for t in kept]
        return kept if len(kept) < 2 or len(set(sets)) == len(sets) else ()

    def resolve(self, targets: Iterable[str] | None = None) -> tuple[list[Edge], list[Unresolved]]:
        edges: list[Edge] = []
        unresolved: list[Unresolved] = []
        for path in (self.source.paths() if targets is None else targets):
            f = self.source.facts(path)
            self.t.begin(path)
            for cache in (self._packages, self._lineage, self._types, self._fqn):
                cache.clear()
            self._resolve_file(path, f, edges, unresolved)
            self.file_deps[path] = (frozenset(self.t.deps), frozenset(self.t.names))
        return edges, unresolved

    def _resolve_file(self, path: str, f: FileFacts, edges: list[Edge], unresolved: list[Unresolved]) -> None:
        seen: set[str] = set()
        for b in f.imports:
            if b.attr == "*" or not b.module:
                continue
            kind, targets = self.fqn(f"{b.module}.{b.attr}")
            for dst in (targets if kind == "facade" else tuple(_file(q) for q in targets)):
                if dst != path and dst not in seen:
                    seen.add(dst)
                    edges.append(Edge(_module(path), _module(dst), "IMPORTS", "import", None, path, 0))
        for d in f.definitions:
            if d.kind not in CONTAINERS:
                continue
            for base in d.bases:
                kind, targets, _, _ = self.type_of_type(base, d.parent or _module(path), path)
                targets = self._visible(path, tuple(t for t in targets if t != d.qname))
                for t in targets if kind == "instance" else ():
                    edges.append(Edge(d.qname, t, "INHERITS", "variant" if len(targets) > 1 else "import",
                                      None, path, d.spans[0][0]))
        for c in f.calls:
            targets, tier, external = self.resolve_call(c, path)
            targets = self._visible(path, tuple(dict.fromkeys(targets)))
            if targets:
                for t in targets:
                    edges.append(Edge(c.caller, t, "CALLS", "variant" if len(targets) > 1 else (tier or "import"),
                                      c.callee, path, c.line))
            elif not external:
                unresolved.append(Unresolved(c.caller, c.callee, c.receiver, path, c.line))
