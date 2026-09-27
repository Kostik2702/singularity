"""TypeScript / JavaScript facts from tree-sitter (CODE-GRAPH P1b).

Same contract as the Python extractor: definitions with qnames, bindings,
call sites and the type hints a resolver without a type checker can use --
annotations, `new X()`, a function's declared return, and the dependency-
injection shape this codebase uses everywhere:

    constructor(@Inject(TOKEN) jobs: JobsRepository) { this.#jobs = jobs; }

`this.#jobs` gets its type from the constructor parameter it was assigned
from (an `alias` hint resolved in the constructor's scope).

Barrels are recorded, not resolved: `export * from './x'` and
`export { a as b } from './y'` land in `exports`; the resolver follows them.
"""
from __future__ import annotations

import re

import tree_sitter as ts
import tree_sitter_javascript as tsj
import tree_sitter_typescript as tst

from .model import CallSite, Definition, FileFacts, ImportBinding, TypeHint

_LANGS = {
    "ts": ts.Language(tst.language_typescript()),
    "tsx": ts.Language(tst.language_tsx()),
    "js": ts.Language(tsj.language()),
}
_PARSERS = {k: ts.Parser(v) for k, v in _LANGS.items()}
_GRAMMAR_BY_SUFFIX = {".ts": "ts", ".mts": "ts", ".cts": "ts", ".tsx": "tsx",
                      ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js"}
_SIG_MAX = 200
_FUNCTION_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}
_FUNCTION_DECLS = {"function_declaration", "generator_function_declaration", "function_signature"}
_CLASS_DECLS = {"class_declaration", "abstract_class_declaration"}
# Names a file mentions (FileFacts.refs). A definition's own name node is never
# pushed onto the walk, so it never lands here.
_REF_KINDS = {"identifier", "property_identifier", "type_identifier", "shorthand_property_identifier",
              "shorthand_property_identifier_pattern"}
_IDENT_STRING = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


def _names_in(node: ts.Node | None) -> set[str]:
    """Every name in a subtree the main walk does not descend into (a class's
    `extends`, an interface body, an export list)."""
    out: set[str] = set()
    todo = [node] if node is not None else []
    while todo:
        n = todo.pop()
        if n.type in _REF_KINDS:
            out.add(_text(n))
        todo.extend(n.children)
    return out


def _text(node: ts.Node | None) -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


def _flat(node: ts.Node | None) -> str:
    return " ".join(_text(node).split())


def _line(node: ts.Node) -> int:
    return node.start_point[0] + 1


def _type_text(node: ts.Node | None) -> str:
    """`: Foo<Bar>` -> `Foo<Bar>`."""
    return _flat(node).lstrip(":").strip()


def _string_value(node: ts.Node | None) -> str:
    if node is None:
        return ""
    frag = [c for c in node.named_children if c.type == "string_fragment"]
    return _text(frag[0]) if frag else _text(node).strip("'\"`")


def _signature(node: ts.Node, src: bytes) -> str:
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    return " ".join(src[node.start_byte:end].decode("utf-8", "replace").split())[:_SIG_MAX]


def _doc1(node: ts.Node) -> str | None:
    """The first line of a `/** ... */` block right above the definition."""
    target = node.parent if node.parent is not None and node.parent.type == "export_statement" else node
    prev = target.prev_named_sibling
    if prev is None or prev.type != "comment" or target.start_point[0] - prev.end_point[0] > 1:
        return None
    raw = _text(prev)
    if not raw.startswith("/**"):
        return None
    for line in raw.strip("/*").splitlines():
        line = line.strip().lstrip("*").strip()
        if line:
            return line[:_SIG_MAX]
    return None


def _decorators(node: ts.Node) -> list[str]:
    out = [_text(c)[1:].strip() for c in node.children if c.type == "decorator"]
    if node.parent is not None and node.parent.type == "export_statement":
        out = [_text(c)[1:].strip() for c in node.parent.children if c.type == "decorator"] + out
    return out


def _is_exported(node: ts.Node) -> bool:
    p = node.parent
    if p is not None and p.type == "export_statement":
        return True
    # `export const f = () => ...`: declarator -> lexical_declaration -> export_statement
    return p is not None and p.parent is not None and p.parent.type == "export_statement"


class _Facts:
    def __init__(self, path: str, src: bytes):
        self.path, self.src = path, src
        self.module = f"{path}::<module>"
        self.defs: dict[str, Definition] = {}
        self.imports: list[ImportBinding] = []
        self.exports: list[ImportBinding] = []
        self.calls: list[CallSite] = []
        self.hints: list[TypeHint] = []
        self.refs: set[str] = set()

    def define(self, node: ts.Node, name: str, kind: str, prefix: str | None, *,
               returns: str | None = None, bases: list[str] | None = None,
               sig_node: ts.Node | None = None) -> str:
        qname = f"{prefix}.{name}" if prefix else f"{self.path}::{name}"
        span = (_line(node), node.end_point[0] + 1)
        if qname in self.defs:                    # overload signatures, declaration merging
            self.defs[qname].spans.append(span)
            if returns and not self.defs[qname].returns:
                self.defs[qname].returns = returns
        else:
            self.defs[qname] = Definition(
                qname=qname, name=name, kind=kind, parent=prefix, spans=[span],
                signature=_signature(sig_node or node, self.src), doc1=_doc1(node),
                decorators=_decorators(node), exported=_is_exported(node),
                bases=bases or [], returns=returns)
        return qname

    def params(self, fn: ts.Node, scope: str, klass: str | None, is_ctor: bool) -> None:
        params = fn.child_by_field_name("parameters")
        if params is None:
            return
        for p in params.named_children:
            if p.type not in ("required_parameter", "optional_parameter"):
                continue
            pattern, ann = p.child_by_field_name("pattern"), p.child_by_field_name("type")
            if pattern is None or pattern.type != "identifier" or ann is None:
                continue
            name, type_expr = _text(pattern), _type_text(ann)
            self.hints.append(TypeHint(scope, name, type_expr, "annotation"))
            property_param = any(c.type in ("accessibility_modifier", "readonly") for c in p.children)
            if is_ctor and klass and property_param:          # `constructor(private x: T)`
                self.hints.append(TypeHint(klass, f"this.{name}", type_expr, "annotation"))

    def value_hint(self, scope: str, target: str, value: ts.Node | None, origin: str | None = None) -> None:
        if value is None:
            return
        while value.type in ("await_expression", "parenthesized_expression", "non_null_expression"):
            inner = value.named_children
            if not inner:
                return
            value = inner[0]
        if value.type == "new_expression":
            ctor = value.child_by_field_name("constructor")
            if ctor is not None:
                self.hints.append(TypeHint(scope, target, _flat(ctor), "ctor", origin))
        elif value.type == "call_expression":
            fn = value.child_by_field_name("function")
            if fn is not None and fn.type in ("identifier", "member_expression"):
                self.hints.append(TypeHint(scope, target, _flat(fn), "call", origin))
        elif value.type == "as_expression":
            named = value.named_children
            if len(named) >= 2:
                self.hints.append(TypeHint(scope, target, _flat(named[-1]), "annotation", origin))
        elif value.type in ("identifier", "member_expression", "this"):
            self.hints.append(TypeHint(scope, target, _flat(value), "alias", origin))


def extract(path: str, src: bytes) -> FileFacts:
    grammar = _GRAMMAR_BY_SUFFIX.get("." + path.rsplit(".", 1)[-1].lower(), "ts")
    root = _PARSERS[grammar].parse(src).root_node
    f = _Facts(path, src)
    # (node, caller scope, definition prefix, enclosing class, enclosing function-is-constructor)
    stack: list[tuple[ts.Node, str, str | None, str | None, bool]] = [(root, f.module, None, None, False)]
    while stack:
        node, scope, prefix, klass, in_ctor = stack.pop()
        kind = node.type
        if kind in _REF_KINDS:
            f.refs.add(_text(node))
            continue
        if kind == "string_fragment" and _IDENT_STRING.match(text := _text(node)):
            f.refs.add(text)              # `obj["handler"]`, a route table keyed by name
            continue

        if kind == "import_statement":
            spec = _string_value(node.child_by_field_name("source"))
            clause = next((c for c in node.named_children if c.type == "import_clause"), None)
            if clause is None:
                f.imports.append(ImportBinding(scope, "", spec, 0, None))       # side effect only
                continue
            for c in clause.named_children:
                if c.type == "identifier":
                    f.imports.append(ImportBinding(scope, _text(c), spec, 0, "default"))
                elif c.type == "namespace_import":
                    ident = next((x for x in c.named_children if x.type == "identifier"), None)
                    f.imports.append(ImportBinding(scope, _text(ident), spec, 0, None))
                elif c.type == "named_imports":
                    for s in c.named_children:
                        if s.type != "import_specifier":
                            continue
                        name = _text(s.child_by_field_name("name"))
                        alias = s.child_by_field_name("alias")
                        f.imports.append(ImportBinding(scope, _text(alias) if alias else name, spec, 0, name))
            continue

        if kind == "export_statement":
            source = node.child_by_field_name("source")
            spec = _string_value(source) if source is not None else ""
            clause = next((c for c in node.named_children if c.type == "export_clause"), None)
            ns_export = next((c for c in node.named_children if c.type == "namespace_export"), None)
            is_default = any(c.type == "default" for c in node.children)
            if clause is not None:
                f.refs |= _names_in(clause)          # `export { helper }` uses `helper`
                for s in clause.named_children:
                    if s.type == "export_specifier":
                        name = _text(s.child_by_field_name("name"))
                        alias = s.child_by_field_name("alias")
                        f.exports.append(ImportBinding(f.module, _text(alias) if alias else name, spec, 0, name))
                if spec:
                    f.imports.append(ImportBinding(f.module, "", spec, 0, None))
            elif ns_export is not None:
                ident = next((x for x in ns_export.named_children if x.type == "identifier"), None)
                f.exports.append(ImportBinding(f.module, _text(ident), spec, 0, None))
                f.imports.append(ImportBinding(f.module, "", spec, 0, None))
            elif source is not None:                                         # export * from
                f.exports.append(ImportBinding(f.module, "*", spec, 0, "*"))
                f.imports.append(ImportBinding(f.module, "", spec, 0, None))
            decl = node.child_by_field_name("declaration")
            value = node.child_by_field_name("value")
            if is_default:
                named = decl if decl is not None else value
                name_node = named.child_by_field_name("name") if named is not None else None
                if name_node is not None:
                    f.exports.append(ImportBinding(f.module, "default", "", 0, _text(name_node)))
                elif value is not None and value.type == "identifier":
                    f.exports.append(ImportBinding(f.module, "default", "", 0, _text(value)))
            for child in reversed(node.named_children):
                if child.type not in ("export_clause", "namespace_export", "string", "decorator"):
                    stack.append((child, scope, prefix, klass, in_ctor))
            continue

        if kind in _FUNCTION_DECLS or kind in _CLASS_DECLS or kind in (
                "interface_declaration", "type_alias_declaration", "enum_declaration",
                "method_definition", "method_signature", "abstract_method_signature"):
            name = _text(node.child_by_field_name("name"))
            if not name:
                for child in reversed(node.children):
                    stack.append((child, scope, prefix, klass, in_ctor))
                continue
            returns = _type_text(node.child_by_field_name("return_type")) or None
            f.refs |= _names_in(node.child_by_field_name("return_type"))     # `(): Result` uses Result
            if kind in _CLASS_DECLS:
                heritage = next((c for c in node.named_children if c.type == "class_heritage"), None)
                bases = []
                if heritage is not None:
                    f.refs |= _names_in(heritage)    # a class used only as a base is used
                    for clause in heritage.named_children:
                        if clause.type == "extends_clause":
                            v = clause.child_by_field_name("value")
                            bases.append(_flat(v) if v is not None else _flat(clause).replace("extends", "", 1).strip())
                qname = f.define(node, name, "class", prefix, bases=bases)
                body = node.child_by_field_name("body")
                if body is not None:
                    stack.append((body, qname, qname, qname, False))
                continue
            if kind in ("interface_declaration", "type_alias_declaration"):
                bases = []
                ext = next((c for c in node.named_children if c.type == "extends_type_clause"), None)
                if ext is not None:
                    bases = [_flat(t) for t in ext.named_children]
                body = node.child_by_field_name("body") or node.child_by_field_name("value")
                f.refs |= _names_in(ext) | _names_in(body)   # types a member or a base names
                qname = f.define(node, name, "interface" if kind == "interface_declaration" else "type",
                                 prefix, bases=bases)
                if body is not None and body.type in ("interface_body", "object_type"):
                    for member in body.named_children:
                        mname = _text(member.child_by_field_name("name"))
                        if member.type == "property_signature" and mname:
                            ann = member.child_by_field_name("type")
                            if ann is not None:
                                f.hints.append(TypeHint(qname, f"this.{mname}", _type_text(ann), "annotation"))
                                if "=>" in _type_text(ann):
                                    # `until: (v: T) => boolean` is called like a method; without
                                    # a node, `options.until(x)` had nothing to land on (P1b audit)
                                    f.define(member, mname, "method", qname)
                        elif member.type == "method_signature" and mname:
                            f.define(member, mname, "method", qname,
                                     returns=_type_text(member.child_by_field_name("return_type")) or None)
                continue
            if kind == "enum_declaration":
                f.define(node, name, "enum", prefix)
                continue
            is_member = kind in ("method_definition", "method_signature", "abstract_method_signature")
            accessor = next((c.type for c in node.children if c.type in ("get", "set")), None)
            member_kind = {"get": "getter", "set": "setter"}.get(accessor or "", "method")
            qname = f.define(node, name, member_kind if is_member else "function", prefix, returns=returns)
            is_ctor = is_member and name == "constructor"
            f.params(node, qname, klass, is_ctor)
            body = node.child_by_field_name("body")
            if body is not None:
                stack.append((body, qname, qname, klass, is_ctor))
            params = node.child_by_field_name("parameters")
            if params is not None:
                stack.append((params, scope, prefix, klass, in_ctor))
            continue

        if kind == "public_field_definition":
            name = _text(node.child_by_field_name("name"))
            ann, value = node.child_by_field_name("type"), node.child_by_field_name("value")
            if klass and name:
                if ann is not None:
                    f.hints.append(TypeHint(klass, f"this.{name}", _type_text(ann), "annotation"))
                if value is not None and value.type in _FUNCTION_VALUES:
                    qname = f.define(node, name, "method", klass,
                                     returns=_type_text(value.child_by_field_name("return_type")) or None,
                                     sig_node=value)
                    f.params(value, qname, klass, False)
                    body = value.child_by_field_name("body")
                    if body is not None:
                        stack.append((body, qname, qname, klass, False))
                    continue
                f.value_hint(klass, f"this.{name}", value, origin=scope)
            if value is not None:
                stack.append((value, scope, prefix, klass, in_ctor))
            continue

        if kind == "variable_declarator":
            name_node, value = node.child_by_field_name("name"), node.child_by_field_name("value")
            ann = node.child_by_field_name("type")
            if name_node is not None and name_node.type == "identifier":
                name = _text(name_node)
                if value is not None and value.type in _FUNCTION_VALUES:
                    qname = f.define(node, name, "function", prefix,
                                     returns=_type_text(value.child_by_field_name("return_type")) or None,
                                     sig_node=value)
                    f.params(value, qname, klass, False)
                    body = value.child_by_field_name("body")
                    if body is not None:
                        stack.append((body, qname, qname, klass, False))
                    continue
                if ann is not None:
                    f.hints.append(TypeHint(scope, name, _type_text(ann), "annotation"))
                f.value_hint(scope, name, value)
            if value is not None:
                stack.append((value, scope, prefix, klass, in_ctor))
            continue

        if kind == "assignment_expression":
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if left is not None and left.type == "member_expression" and klass:
                obj = left.child_by_field_name("object")
                if obj is not None and obj.type == "this":
                    target = f"this.{_text(left.child_by_field_name('property'))}"
                    f.value_hint(klass, target, right, origin=scope)

        elif kind == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type == "identifier":
                f.calls.append(CallSite(scope, _line(node), _text(fn), None, klass))
            elif fn is not None and fn.type == "member_expression":
                prop = fn.child_by_field_name("property")
                obj = fn.child_by_field_name("object")
                if prop is not None and obj is not None:
                    f.calls.append(CallSite(scope, _line(node), _text(prop), _flat(obj), klass))

        elif kind == "new_expression":
            ctor = node.child_by_field_name("constructor")
            if ctor is not None and ctor.type == "identifier":
                f.calls.append(CallSite(scope, _line(node), _text(ctor), None, klass))
            elif ctor is not None and ctor.type == "member_expression":
                prop, obj = ctor.child_by_field_name("property"), ctor.child_by_field_name("object")
                if prop is not None and obj is not None:
                    f.calls.append(CallSite(scope, _line(node), _text(prop), _flat(obj), klass))

        for child in reversed(node.children):
            stack.append((child, scope, prefix, klass, in_ctor))

    return FileFacts(path=path, lang="ts", degraded=root.has_error, definitions=list(f.defs.values()),
                     imports=f.imports, calls=f.calls, hints=f.hints, exports=f.exports,
                     refs=sorted(f.refs))
