"""TypeScript / JavaScript resolution without a type checker (CODE-GRAPH P1b).

Same tiers and the same refusal as the Python resolver (see resolve_py):
a call provably into the platform or a library draws nothing, a call the
rules cannot place goes to `unresolved`.

What TypeScript adds:
* module specifiers: relative paths with extension substitution (`./x.js`
  may be `./x.ts`), `index.*`, workspace packages by `package.json` name and
  `exports` conditions -- whatever they are called (`tkh-source` here) -- and
  tsconfig `paths`;
* barrels: `export *` and `export {a as b} from` are followed to the file
  that DEFINES the symbol;
* member types: `this.#deps.metrics.listAll()` walks a class field's
  annotation, then an interface property's, then finds the method.
"""
from __future__ import annotations

import json
import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass, field

from .model import CallSite, Definition, FileFacts, ImportBinding
from .resolve_py import EXTERNAL, UNKNOWN, Edge, Unresolved
from .stoplist import TS_GLOBALS, TS_STOPLIST

_MAX_HOPS = 8
_CODE_EXT = (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
_JS_TO_TS = {".js": (".ts", ".tsx"), ".jsx": (".tsx",), ".mjs": (".mts", ".ts"), ".cjs": (".cts", ".ts")}
_INDEX = ("index.ts", "index.tsx", "index.d.ts", "index.js", "index.jsx", "index.mjs")
_TYPELIKE = ("class", "interface", "type", "enum")


@dataclass
class Package:
    name: str
    dir: str                                   # unit-relative, '' for the root
    exports: dict[str, list[str]] = field(default_factory=dict)   # subpath -> candidate targets
    fallback: list[str] = field(default_factory=list)


def _leaves(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for v in value.values():
            out.extend(_leaves(v))
        return out
    if isinstance(value, list):
        return [x for v in value for x in _leaves(v)]
    return []


def strip_jsonc(text: str) -> str:
    """tsconfig allows comments and trailing commas; json does not."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            out.append(ch)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        else:
            out.append(ch)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_workspace(read, manifests: list[str]) -> tuple[list[Package], list[tuple[str, list[str]]]]:
    """Packages from every package.json and path aliases from tsconfig files.
    `read(rel) -> str | None` reads a unit-relative file."""
    packages: list[Package] = []
    aliases: list[tuple[str, list[str]]] = []
    for rel in manifests:
        base = rel.rsplit("/", 1)[-1]
        text = read(rel)
        if text is None:
            continue
        try:
            data = json.loads(strip_jsonc(text))
        except ValueError:
            continue
        d = rel.rsplit("/", 1)[0] if "/" in rel else ""
        if base == "package.json" and isinstance(data, dict) and isinstance(data.get("name"), str):
            pkg = Package(name=data["name"], dir=d)
            exp = data.get("exports")
            if isinstance(exp, (str, list)) or (isinstance(exp, dict) and not any(k.startswith(".") for k in exp)):
                pkg.exports["."] = _leaves(exp)
            elif isinstance(exp, dict):
                for sub, target in exp.items():
                    pkg.exports[sub] = _leaves(target)
            pkg.fallback = [v for k in ("types", "typings", "module", "main")
                            if isinstance(v := data.get(k), str)] + ["src/index", "index"]
            packages.append(pkg)
        elif base.startswith("tsconfig") and isinstance(data, dict):
            opts = data.get("compilerOptions") or {}
            base_url = posixpath.normpath(posixpath.join(d, opts.get("baseUrl") or "."))
            for pattern, targets in (opts.get("paths") or {}).items():
                if isinstance(targets, list):
                    aliases.append((pattern, [posixpath.normpath(posixpath.join(base_url, t)) for t in targets]))
    packages.sort(key=lambda p: -len(p.name))
    return packages, aliases


def _module_scope(path: str) -> str:
    return f"{path}::<module>"


class TsResolver:
    def __init__(self, facts: dict[str, FileFacts], test_paths: set[str] | frozenset[str] = frozenset(),
                 packages: list[Package] | None = None,
                 aliases: list[tuple[str, list[str]]] | None = None):
        self.facts, self.test_paths = facts, test_paths
        self.packages, self.aliases = packages or [], aliases or []
        self.defs: dict[str, Definition] = {}
        self.def_file: dict[str, str] = {}
        self.members: dict[str, dict[str, str]] = defaultdict(dict)
        self.by_name: dict[str, list[str]] = defaultdict(list)
        self.bindings: dict[str, dict[str, ImportBinding]] = defaultdict(dict)
        self.exports: dict[str, list[ImportBinding]] = defaultdict(list)
        self.hints: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        for path, f in facts.items():
            for d in f.definitions:
                self.defs[d.qname] = d
                self.def_file[d.qname] = path
                self.members[d.parent or _module_scope(path)].setdefault(d.name, d.qname)
                self.by_name[d.name].append(d.qname)
            for b in f.imports:
                if b.local:
                    self.bindings[b.scope].setdefault(b.local, b)
            self.exports[path] = list(f.exports)
            for h in f.hints:
                self.hints[h.scope][h.target].append(h)
        self._mod_cache: dict[tuple[str, str], str | None] = {}
        self._reach_cache: dict[str, set[str | None]] = {}

    # --- modules ---------------------------------------------------------------

    def _file_for(self, base: str) -> str | None:
        if not base or base.startswith(".."):
            return None
        if base in self.facts:
            return base
        stem, ext = posixpath.splitext(base)
        for alt in _JS_TO_TS.get(ext, ()):
            if stem + alt in self.facts:
                return stem + alt
        for ext2 in _CODE_EXT:
            if base + ext2 in self.facts:
                return base + ext2
        for idx in _INDEX:
            cand = f"{base}/{idx}" if base != "." else idx
            if cand in self.facts:
                return cand
        return None

    def module_path(self, from_path: str, spec: str) -> str | None:
        key = (from_path, spec)
        if key in self._mod_cache:
            return self._mod_cache[key]
        found: str | None = None
        if spec.startswith("."):
            found = self._file_for(posixpath.normpath(posixpath.join(posixpath.dirname(from_path), spec)))
        else:
            found = self._package_file(spec) or self._alias_file(spec)
        self._mod_cache[key] = found
        return found

    def _package_file(self, spec: str) -> str | None:
        for pkg in self.packages:
            if spec == pkg.name:
                sub = "."
            elif spec.startswith(pkg.name + "/"):
                sub = "./" + spec[len(pkg.name) + 1:]
            else:
                continue
            targets = list(pkg.exports.get(sub, []))
            for pattern, t in pkg.exports.items():
                if "*" in pattern and sub.startswith(pattern.split("*")[0]):
                    rest = sub[len(pattern.split("*")[0]):]
                    targets += [x.replace("*", rest) for x in t]
            if sub == ".":
                targets += pkg.fallback
            else:
                targets += [sub, "./src/" + sub[2:]]
            ranked = sorted(targets, key=lambda t: 0 if t.endswith((".ts", ".tsx")) else 1)
            for t in ranked:
                hit = self._file_for(posixpath.normpath(posixpath.join(pkg.dir, t)))
                if hit:
                    return hit
            return None
        return None

    def _alias_file(self, spec: str) -> str | None:
        for pattern, targets in self.aliases:
            if "*" in pattern:
                head, _, tail = pattern.partition("*")
                if spec.startswith(head) and spec.endswith(tail):
                    mid = spec[len(head): len(spec) - len(tail) if tail else None]
                    for t in targets:
                        hit = self._file_for(t.replace("*", mid))
                        if hit:
                            return hit
            elif spec == pattern:
                for t in targets:
                    hit = self._file_for(t)
                    if hit:
                        return hit
        return None

    def resolve_binding(self, from_path: str, b: ImportBinding, hops: int = 0) -> tuple[str | None, str | None]:
        if not b.local or hops > _MAX_HOPS:
            return UNKNOWN
        target = self.module_path(from_path, b.module)
        if target is None:
            return EXTERNAL
        if b.attr is None:
            return ("module", target)
        return self.symbol_in(target, b.attr, hops + 1)

    def symbol_in(self, path: str, name: str, hops: int = 0) -> tuple[str | None, str | None]:
        if hops > _MAX_HOPS:
            return UNKNOWN
        mod = _module_scope(path)
        q = self.members[mod].get(name)
        if q:
            return ("symbol", q)
        for e in self.exports.get(path, []):
            if e.local != name:
                continue
            if not e.module:                                   # export { a as name } / export default a
                if e.attr and e.attr != name:
                    found = self.symbol_in(path, e.attr, hops + 1)
                    if found[0]:
                        return found
                b = self.bindings[mod].get(e.attr or name)
                return self.resolve_binding(path, b, hops + 1) if b else UNKNOWN
            target = self.module_path(path, e.module)
            if target is None:
                return EXTERNAL
            if e.attr is None:
                return ("module", target)
            return self.symbol_in(target, e.attr, hops + 1)
        b = self.bindings[mod].get(name)
        if b is not None:
            return self.resolve_binding(path, b, hops + 1)
        for e in self.exports.get(path, []):
            if e.local == "*":
                target = self.module_path(path, e.module)
                if target and target != path:
                    found = self.symbol_in(target, name, hops + 1)
                    if found[0] == "symbol":
                        return found
        return UNKNOWN

    # --- names, types, members ------------------------------------------------

    def _chain(self, scope: str, path: str) -> list[str]:
        out: list[str] = []
        q: str | None = scope
        while q and q in self.defs:
            if self.defs[q].kind not in _TYPELIKE:
                out.append(q)
            q = self.defs[q].parent
        out.append(_module_scope(path))
        return out

    def lookup_name(self, name: str, scope: str, path: str) -> tuple[str | None, str | None, str | None]:
        for s in self._chain(scope, path):
            q = self.members[s].get(name)
            if q:
                return ("symbol", q, "scope")
            b = self.bindings[s].get(name)
            if b is not None:
                kind, target = self.resolve_binding(path, b)
                return (kind, target, "import")
        if name in TS_GLOBALS:
            return ("external", None, None)
        return (None, None, None)

    @staticmethod
    def _type_name(expr: str) -> str | None:
        t = expr.strip()
        for prefix in ("readonly ", "typeof ", "keyof "):
            if t.startswith(prefix):
                t = t[len(prefix):]
        parts = [p.strip() for p in t.split("|") if p.strip() not in ("null", "undefined", "void", "")]
        if not parts:
            return None
        t = parts[0]
        while t.startswith(("Promise<", "Readonly<", "Awaited<")) and t.endswith(">"):
            t = t[t.index("<") + 1:-1].strip()
        if t.endswith("[]"):
            return None
        t = t.split("<", 1)[0].strip()
        return t if re.fullmatch(r"[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)*", t) else None

    def _defn_kind(self, q: str) -> str:
        k = self.defs[q].kind
        return "class" if k in _TYPELIKE else k

    def type_of_type(self, expr: str, scope: str, path: str) -> tuple[str | None, str | None]:
        """A TYPE expression -> ('instance', qname) | EXTERNAL | UNKNOWN."""
        name = self._type_name(expr)
        if name is None:
            return UNKNOWN
        head, _, rest = name.partition(".")
        kind, target, _ = self.lookup_name(head, scope, path)
        for part in rest.split(".") if rest else []:
            if kind == "module":
                kind, target = self.symbol_in(target, part)
            else:
                return EXTERNAL if kind == "external" else UNKNOWN
        if kind == "symbol" and target in self.defs and self._defn_kind(target) == "class":
            return ("instance", target)
        if kind == "external":
            return EXTERNAL
        return UNKNOWN

    def _hint_type(self, h, scope_for_expr: str, path: str, klass: str | None, depth: int
                   ) -> tuple[str | None, str | None, str | None]:
        origin = h.origin or scope_for_expr
        opath = self._file_of_scope(origin, path)
        if h.source == "annotation":
            kind, target = self.type_of_type(h.type_expr, origin, opath)
            return (kind, target, "annotation")
        if h.source == "ctor":
            kind, target, _ = self.type_of(h.type_expr, origin, opath, klass, depth + 1)
            if kind == "class":
                return ("instance", target, "local-type")
            return (kind if kind == "external" else None, None, None)
        if h.source == "call":
            kind, target, _ = self.type_of(h.type_expr, origin, opath, klass, depth + 1)
            if kind in ("function", "method") and target and self.defs[target].returns:
                d = self.defs[target]
                rkind, rtarget = self.type_of_type(d.returns, d.parent or _module_scope(self.def_file[target]),
                                                   self.def_file[target])
                return (rkind, rtarget, "local-type")
            return ("external", None, None) if kind == "external" else (None, None, None)
        if h.source == "alias":
            return self.type_of(h.type_expr, origin, opath, klass, depth + 1)
        return (None, None, None)

    def _file_of_scope(self, scope: str, default: str) -> str:
        if scope in self.def_file:
            return self.def_file[scope]
        return scope.split("::", 1)[0] if "::" in scope else default

    def lineage(self, klass: str) -> tuple[list[str], bool]:
        order, external, todo = [], False, [klass]
        while todo:
            c = todo.pop(0)
            if c in order:
                continue
            order.append(c)
            d = self.defs[c]
            path = self.def_file[c]
            for base in d.bases:
                kind, target = self.type_of_type(base, d.parent or _module_scope(path), path)
                if kind == "instance" and target:
                    todo.append(target)
                else:
                    external = True
        return order, external

    def member_type(self, klass: str, name: str, depth: int) -> tuple[str | None, str | None, str | None]:
        order, external = self.lineage(klass)
        for c in order:
            for h in self.hints[c].get(f"this.{name}", []):
                kind, target, _ = self._hint_type(h, c, self.def_file[c], c, depth)
                if kind:
                    return (kind, target, "attr-type")
            if name in self.members[c]:
                return ("member", self.members[c][name], None)
        return ("external", None, None) if external else (None, None, None)

    def type_of(self, expr: str, scope: str, path: str, klass: str | None, depth: int = 0
                ) -> tuple[str | None, str | None, str | None]:
        if depth > _MAX_HOPS or not expr or "(" in expr or "[" in expr:
            return (None, None, None)
        parts = expr.replace("?.", ".").split(".")
        head = parts[0]
        if head == "this":
            if not klass or klass not in self.defs:
                return (None, None, None)
            kind, target, tier = "instance", klass, "self"
        else:
            kind, target, tier = None, None, None
            for s in self._chain(scope, path):
                for h in self.hints[s].get(head, []):
                    kind, target, tier = self._hint_type(h, s, path, klass, depth)
                    if kind:
                        break
                if kind:
                    break
            if not kind:
                kind, target, tier = self.lookup_name(head, scope, path)
                if kind == "symbol" and target in self.defs:
                    kind = self._defn_kind(target)
        for part in parts[1:]:
            if kind == "module":
                kind, target = self.symbol_in(target, part)
                if kind == "symbol" and target in self.defs:
                    kind = self._defn_kind(target)
                continue
            if kind in ("instance", "class") and target in self.defs:
                kind, target, t2 = self.member_type(target, part, depth + 1)
                tier = t2 or tier
                continue
            if kind == "external":
                return ("external", None, None)
            return (None, None, None)
        return (kind, target, tier)

    # --- calls --------------------------------------------------------------

    def resolve_call(self, c: CallSite, path: str) -> tuple[str | None, str | None, bool]:
        if c.receiver is None:
            kind, target, tier = self.lookup_name(c.callee, c.caller, path)
            if kind == "symbol" and target:
                return target, tier, False
            if kind in ("external", "module"):
                return None, None, kind == "external"
            return self._guess(c.callee, path)
        if c.receiver in ("this", "super") and c.klass in self.defs:
            order, external = self.lineage(c.klass)
            for cls in order[1:] if c.receiver == "super" else order:
                q = self.members[cls].get(c.callee)
                if q:
                    return (q, "self", False) if self._callable(q) else (None, None, False)
            return None, None, external
        kind, target, tier = self.type_of(c.receiver, c.caller, path, c.klass)
        if kind == "module" and target:
            found = self.symbol_in(target, c.callee)
            if found[0] == "symbol":
                return found[1], "import", False
            return (None, None, True) if found == EXTERNAL else self._guess(c.callee, path)
        if kind in ("instance", "class") and target in self.defs:
            order, external = self.lineage(target)
            for cls in order:
                q = self.members[cls].get(c.callee)
                if q:
                    if not self._callable(q):
                        return None, None, False
                    return q, ("class-attr" if kind == "class" else (tier or "annotation")), False
            return None, None, external
        if kind == "external":
            return None, None, True
        if "(" in c.receiver:
            # the receiver is the RESULT of a call chain (`db.select()...orderBy()`):
            # nothing is known of its type, and a method name that happens to be
            # unique in our code is no evidence (P1b audit: drizzle `.limit()`)
            return None, None, False
        return self._guess(c.callee, path)

    def _callable(self, q: str) -> bool:
        """A getter or setter is read or assigned, never called as `x.name()`."""
        return self.defs[q].kind not in ("getter", "setter")

    def _package_of(self, path: str) -> str | None:
        best = None
        for pkg in self.packages:
            d = pkg.dir
            if (d == "" or path == d or path.startswith(d + "/")) and (best is None or len(d) > len(best)):
                best = d
        return best

    def _reachable_packages(self, path: str) -> set[str | None]:
        """The caller's own package plus every package its file imports from."""
        if path not in self._reach_cache:
            reach = {self._package_of(path)}
            for b in self.facts[path].imports:
                dst = self.module_path(path, b.module)
                if dst:
                    reach.add(self._package_of(dst))
            self._reach_cache[path] = reach
        return self._reach_cache[path]

    def _is_local(self, qname: str) -> bool:
        q = self.defs[qname].parent
        while q and q in self.defs:
            if self.defs[q].kind not in _TYPELIKE:
                return True
            q = self.defs[q].parent
        return False

    def _guess(self, name: str, caller_path: str) -> tuple[str | None, str | None, bool]:
        """The weakest tier. Hardened after two out-of-sample audits found it
        right 9 times in 14: never a type, getter or setter; never a test's
        definition from production code; never a definition in a package the
        caller's file neither belongs to nor imports from."""
        if name in TS_STOPLIST or name in TS_GLOBALS:
            return None, None, False
        from_test = caller_path in self.test_paths
        hits = [q for q in self.by_name.get(name, [])
                if not self._is_local(q) and (from_test or self.def_file[q] not in self.test_paths)
                and self.defs[q].kind not in ("interface", "type", "enum", "getter", "setter")]
        if len(hits) != 1:
            return None, None, False
        if self.packages and self._package_of(self.def_file[hits[0]]) not in self._reachable_packages(caller_path):
            return None, None, False
        return hits[0], "name-unique", False

    def resolve(self) -> tuple[list[Edge], list[Unresolved]]:
        edges: list[Edge] = []
        unresolved: list[Unresolved] = []
        for path, f in self.facts.items():
            seen: set[str] = set()
            for b in f.imports:
                dst = self.module_path(path, b.module)
                if dst and dst != path and dst not in seen:
                    seen.add(dst)
                    edges.append(Edge(_module_scope(path), _module_scope(dst), "IMPORTS", "import", None, path, 0))
            for d in f.definitions:
                if d.kind != "class":
                    continue
                for base in d.bases:
                    kind, target = self.type_of_type(base, d.parent or _module_scope(path), path)
                    if kind == "instance" and target and target != d.qname:
                        edges.append(Edge(d.qname, target, "INHERITS", "import", None, path, d.spans[0][0]))
            for c in f.calls:
                target, tier, external = self.resolve_call(c, path)
                if target:
                    edges.append(Edge(c.caller, target, "CALLS", tier or "import", c.callee, path, c.line))
                elif not external:
                    unresolved.append(Unresolved(c.caller, c.callee, c.receiver, path, c.line))
        return edges, unresolved
