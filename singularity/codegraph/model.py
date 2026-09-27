"""What an extractor reports about one file, before anything is resolved.

Plain data, no tree-sitter types: the extractor is shared with a future
chunker (CODE-GRAPH D-10) and with a process pool, so everything here must be
picklable and free of parser objects.
"""
from __future__ import annotations

import json
import zlib
from dataclasses import dataclass, field


@dataclass
class Definition:
    qname: str                 # `<unit-relative path>::<Outer>.<inner>`
    name: str
    kind: str                  # function | method | class
    parent: str | None         # enclosing definition's qname
    spans: list[tuple[int, int]]   # 1-based line ranges; several for a merged redefinition
    signature: str
    doc1: str | None
    decorators: list[str]
    exported: bool
    bases: list[str] = field(default_factory=list)   # class only: base expressions as written
    returns: str | None = None     # declared return type, as written


@dataclass
class ImportBinding:
    scope: str                 # qname of the scope the import is visible in
    local: str                 # the name code in that scope uses
    module: str                # dotted module as written (no leading dots)
    level: int                 # relative-import depth; 0 = absolute
    attr: str | None           # `from m import attr`; None for `import m`


@dataclass
class CallSite:
    caller: str                # qname of the innermost enclosing definition, or `<path>::<module>`
    line: int
    callee: str                # the called name
    receiver: str | None       # `obj` of `obj.callee(...)`, as written; None for a bare call
    klass: str | None          # enclosing class qname, for `self`/`cls`


@dataclass
class TypeHint:
    scope: str                 # function qname, or class qname for `self.x`
    target: str                # `x` or `self.x`
    type_expr: str             # constructor called, annotation written, function called, or name aliased
    source: str                # ctor | annotation | call (a function's declared return) | alias
    origin: str | None = None  # scope in which `type_expr` is resolved, when not `scope` itself
                               # (`this.#x = x` in a constructor: `x` is the constructor's)


@dataclass
class FileFacts:
    path: str
    lang: str
    degraded: bool             # the parser recovered from errors somewhere in the file
    definitions: list[Definition]
    imports: list[ImportBinding]
    calls: list[CallSite]
    hints: list[TypeHint]
    # What the file makes importable beyond its own top-level definitions:
    # re-exports (`export { a as b } from './m'`, `export * from './m'`) and
    # renamed local exports (`export { a as b }`, `export default X`).
    # `local` = the exported name, `module` = the source specifier ('' for a
    # local export), `attr` = the original name, '*' for a star re-export.
    exports: list[ImportBinding] = field(default_factory=list)


# --- stored form ---------------------------------------------------------------
# An update re-resolves a few files against the rest of the unit without
# parsing the rest again: each file's facts are stored with the graph. JSON,
# not pickle -- a graph file must never be able to run code when it is read.

def _binding(b: ImportBinding) -> list:
    return [b.scope, b.local, b.module, b.level, b.attr]


def encode_facts(f: FileFacts) -> bytes:
    return zlib.compress(json.dumps([
        f.path, f.lang, f.degraded,
        [[d.qname, d.name, d.kind, d.parent, d.spans, d.signature, d.doc1, d.decorators, d.exported,
          d.bases, d.returns] for d in f.definitions],
        [_binding(b) for b in f.imports],
        [[c.caller, c.line, c.callee, c.receiver, c.klass] for c in f.calls],
        [[h.scope, h.target, h.type_expr, h.source, h.origin] for h in f.hints],
        [_binding(b) for b in f.exports],
    ], separators=(",", ":"), ensure_ascii=False).encode("utf-8"), 1)


def decode_facts(blob: bytes) -> FileFacts:
    path, lang, degraded, defs, imports, calls, hints, exports = json.loads(zlib.decompress(blob))
    return FileFacts(
        path=path, lang=lang, degraded=degraded,
        definitions=[Definition(q, n, k, p, [tuple(s) for s in spans], sig, doc, dec, exp, bases, ret)
                     for q, n, k, p, spans, sig, doc, dec, exp, bases, ret in defs],
        imports=[ImportBinding(*b) for b in imports],
        calls=[CallSite(*c) for c in calls],
        hints=[TypeHint(*h) for h in hints],
        exports=[ImportBinding(*b) for b in exports])
