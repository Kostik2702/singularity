"""Java facts from tree-sitter (CODE-GRAPH D3).

Same contract as the other extractors (see model). What Java adds:
* the package: a type's fully qualified name is `<package>.<Name>` wherever
  the file sits, so the resolver finds a type by it, not by the file's path;
* imports as bindings -- `import a.b.C` binds C to module `a.b`, attribute C;
  `import a.b.*` binds `*`; `import static a.b.C.m` is the same with level 1
  (a static member of a type, not a type of a package);
* one definition `<Class>.constructor` for every constructor of a class
  (overloads merge, as same-named definitions of one scope always do), and
  `new C()` is a call of `C`;
* a record's components are its accessor methods (`p.x()`).

An anonymous class (`new Runnable() { ... }`) is not a definition. Its
methods' calls belong to the named method that creates it, and a bare call of
a name the anonymous body declares itself is recorded on `this` of no known
class -- never resolved against the enclosing class, which does not declare it.

Annotations are the decorators. `exported` is always false: nothing is
imported past the unit the way a TypeScript export is (entry points --
`main`, Android components -- are analysis's business).
"""
from __future__ import annotations

import re

import tree_sitter as ts
import tree_sitter_java as tsj

from .model import CallSite, Definition, FileFacts, ImportBinding, TypeHint

_PARSER = ts.Parser(ts.Language(tsj.language()))
_SIG_MAX = 200
_TYPE_DECLS = {"class_declaration": "class", "interface_declaration": "interface", "enum_declaration": "enum",
               "record_declaration": "class", "annotation_type_declaration": "interface"}
_REF_KINDS = {"identifier", "type_identifier"}
_IDENT_STRING = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_NO_ANON: frozenset[str] = frozenset()
_BOM = "﻿".encode()


def _text(node: ts.Node | None) -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


def _flat(node: ts.Node | None) -> str:
    return " ".join(_text(node).split())


def _line(node: ts.Node) -> int:
    return node.start_point[0] + 1


def _names_in(node: ts.Node | None) -> set[str]:
    out: set[str] = set()
    todo = [node] if node is not None else []
    while todo:
        n = todo.pop()
        if n.type in _REF_KINDS:
            out.add(_text(n))
        todo.extend(n.children)
    return out


def _signature(node: ts.Node, src: bytes) -> str:
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    return " ".join(src[node.start_byte:end].decode("utf-8", "replace").split())[:_SIG_MAX]


def _doc1(node: ts.Node) -> str | None:
    """The first line of a `/** ... */` block right above the definition."""
    prev = node.prev_named_sibling
    if prev is None or prev.type != "block_comment" or node.start_point[0] - prev.end_point[0] > 1:
        return None
    raw = _text(prev)
    if not raw.startswith("/**"):
        return None
    for line in raw.strip("/*").splitlines():
        line = line.strip().lstrip("*").strip()
        if line:
            return line[:_SIG_MAX]
    return None


def _annotations(node: ts.Node) -> list[str]:
    mods = next((c for c in node.children if c.type == "modifiers"), None)
    if mods is None:
        return []
    return [_text(a)[1:].strip() for a in mods.named_children if a.type in ("marker_annotation", "annotation")]


def _modifier_names(node: ts.Node) -> set[str]:
    """Names in a declaration's annotations: an annotation type used only as
    `@Marker` is used."""
    return {n for c in node.children if c.type == "modifiers" for n in _names_in(c)}


def _type_text(node: ts.Node | None) -> str:
    return _flat(node)


def _simple_type(node: ts.Node | None) -> tuple[str, str | None]:
    """(simple name, qualifier) of a class type as written: `a.b.C<T>` ->
    ('C', 'a.b'); `C` -> ('C', None)."""
    if node is not None and node.type == "generic_type":
        node = next((c for c in node.named_children if c.type in ("type_identifier", "scoped_type_identifier")),
                    None)
    text = _flat(node)
    qualifier, _, simple = text.rpartition(".")
    return simple, qualifier or None


class _Facts:
    def __init__(self, path: str, src: bytes):
        self.path, self.src = path, src
        self.module = f"{path}::<module>"
        self.package: str | None = None
        self.defs: dict[str, Definition] = {}
        self.imports: list[ImportBinding] = []
        self.calls: list[CallSite] = []
        self.hints: list[TypeHint] = []
        self.refs: set[str] = set()

    def define(self, node: ts.Node, name: str, kind: str, prefix: str | None, *,
               returns: str | None = None, bases: list[str] | None = None) -> str:
        qname = f"{prefix}.{name}" if prefix else f"{self.path}::{name}"
        span = (_line(node), node.end_point[0] + 1)
        self.refs |= _modifier_names(node)
        if qname in self.defs:                    # overloads
            self.defs[qname].spans.append(span)
            self.defs[qname].decorators.extend(d for d in _annotations(node)
                                               if d not in self.defs[qname].decorators)
            if returns and not self.defs[qname].returns:
                self.defs[qname].returns = returns
        else:
            self.defs[qname] = Definition(
                qname=qname, name=name, kind=kind, parent=prefix, spans=[span],
                signature=_signature(node, self.src), doc1=_doc1(node),
                decorators=_annotations(node), exported=False, bases=bases or [], returns=returns)
        return qname

    def params(self, fn: ts.Node, scope: str) -> None:
        params = fn.child_by_field_name("parameters")
        if params is None:
            return
        for p in params.named_children:
            if p.type == "formal_parameter":
                name, typ = p.child_by_field_name("name"), p.child_by_field_name("type")
            elif p.type == "spread_parameter":            # `String... args`: an array, no member calls
                continue
            else:
                continue
            if name is not None and typ is not None:
                self.hints.append(TypeHint(scope, _text(name), _type_text(typ), "annotation"))

    def value_hint(self, scope: str, target: str, value: ts.Node | None, origin: str | None = None) -> None:
        while value is not None and value.type in ("parenthesized_expression", "cast_expression"):
            if value.type == "cast_expression":
                self.hints.append(TypeHint(scope, target, _type_text(value.child_by_field_name("type")),
                                           "annotation", origin))
                return
            value = value.named_children[0] if value.named_children else None
        if value is None:
            return
        if value.type == "object_creation_expression":
            simple, qualifier = _simple_type(value.child_by_field_name("type"))
            self.hints.append(TypeHint(scope, target, f"{qualifier}.{simple}" if qualifier else simple,
                                       "ctor", origin))
        elif value.type == "method_invocation":
            obj, name = value.child_by_field_name("object"), value.child_by_field_name("name")
            if name is not None and (obj is None or "(" not in _text(obj)):
                expr = f"{_flat(obj)}.{_text(name)}" if obj is not None else _text(name)
                self.hints.append(TypeHint(scope, target, expr, "call", origin))
        elif value.type in ("identifier", "field_access", "this"):
            self.hints.append(TypeHint(scope, target, _flat(value), "alias", origin))


def extract(path: str, src: bytes) -> FileFacts:
    src = src.removeprefix(_BOM)                     # a byte-order mark would read as a syntax error
    root = _PARSER.parse(src).root_node
    f = _Facts(path, src)
    # (node, caller scope, definition prefix, enclosing class, names an enclosing anonymous body declares)
    stack: list[tuple[ts.Node, str, str | None, str | None, frozenset[str]]] = [
        (root, f.module, None, None, _NO_ANON)]
    while stack:
        node, scope, prefix, klass, anon = stack.pop()
        kind = node.type
        if kind in _REF_KINDS:
            f.refs.add(_text(node))
            continue
        if kind == "string_fragment" and _IDENT_STRING.match(text := _text(node)):
            f.refs.add(text)                  # a reflective lookup, a route table keyed by name
            continue
        if kind in ("line_comment", "block_comment"):
            continue

        if kind == "package_declaration":
            name = next((c for c in node.named_children if c.type in ("scoped_identifier", "identifier")), None)
            f.package = _text(name) or None
            continue

        if kind == "import_declaration":
            static = any(c.type == "static" for c in node.children)
            star = any(c.type == "asterisk" for c in node.children)
            name = next((c for c in node.named_children if c.type in ("scoped_identifier", "identifier")), None)
            dotted = _text(name)
            if star:
                f.imports.append(ImportBinding(f.module, "*", dotted, int(static), "*"))
            elif dotted:
                module, _, attr = dotted.rpartition(".")
                f.imports.append(ImportBinding(f.module, attr, module, int(static), attr))
            continue

        if kind in _TYPE_DECLS:
            name = _text(node.child_by_field_name("name"))
            if not name:
                continue
            bases: list[str] = []
            sup = node.child_by_field_name("superclass")
            if sup is not None and sup.named_children:
                bases.append(_type_text(sup.named_children[0]))
            for clause in node.named_children:
                if clause.type in ("super_interfaces", "extends_interfaces"):
                    for lst in clause.named_children:
                        bases += [_type_text(t) for t in (lst.named_children if lst.type == "type_list" else [lst])]
            f.refs |= _names_in(sup) | {n for c in node.named_children
                                        if c.type in ("super_interfaces", "extends_interfaces")
                                        for n in _names_in(c)}
            qname = f.define(node, name, _TYPE_DECLS[kind], prefix, bases=bases)
            if kind == "record_declaration":
                components = node.child_by_field_name("parameters")
                for p in components.named_children if components is not None else []:
                    pname, ptype = p.child_by_field_name("name"), p.child_by_field_name("type")
                    if pname is not None:
                        f.define(p, _text(pname), "method", qname, returns=_type_text(ptype) or None)
                        if ptype is not None:
                            f.hints.append(TypeHint(qname, f"this.{_text(pname)}", _type_text(ptype), "annotation"))
                            f.refs |= _names_in(ptype)
            body = node.child_by_field_name("body")
            if body is not None:
                if kind == "enum_declaration":    # `Kind.A.next()`: a constant is an instance of its enum
                    for c in body.named_children:
                        if c.type == "enum_constant" and (cname := _text(c.child_by_field_name("name"))):
                            f.hints.append(TypeHint(qname, f"this.{cname}", name, "annotation"))
                stack.append((body, qname, qname, qname, _NO_ANON))
            continue

        if kind in ("method_declaration", "constructor_declaration", "compact_constructor_declaration"):
            body = node.child_by_field_name("body")
            ret = node.child_by_field_name("type")
            f.refs |= _names_in(ret) | _names_in(node.child_by_field_name("parameters"))
            if anon:                              # a method of an anonymous class: not a definition
                f.refs |= _modifier_names(node)
                f.params(node, scope)
                if body is not None:
                    stack.append((body, scope, prefix, klass, anon))
                continue
            is_ctor = kind != "method_declaration"
            name = "constructor" if is_ctor else _text(node.child_by_field_name("name"))
            if not name:
                continue
            returns = None if is_ctor or ret is None or ret.type == "void_type" else _type_text(ret)
            qname = f.define(node, name, "method", prefix, returns=returns)
            f.params(node, qname)
            if body is not None:
                stack.append((body, qname, qname, klass, _NO_ANON))
            continue

        if kind == "field_declaration":
            typ = node.child_by_field_name("type")
            f.refs |= _names_in(typ) | _modifier_names(node)
            for decl in node.named_children:
                if decl.type != "variable_declarator":
                    continue
                name, value = _text(decl.child_by_field_name("name")), decl.child_by_field_name("value")
                if klass and not anon and name:
                    if typ is not None:
                        f.hints.append(TypeHint(klass, f"this.{name}", _type_text(typ), "annotation"))
                    f.value_hint(klass, f"this.{name}", value, origin=scope)
                if value is not None:
                    stack.append((value, scope, prefix, klass, anon))
            continue

        if kind == "local_variable_declaration":
            typ = node.child_by_field_name("type")
            f.refs |= _names_in(typ) - {"var"}
            for decl in node.named_children:
                if decl.type != "variable_declarator":
                    continue
                name, value = _text(decl.child_by_field_name("name")), decl.child_by_field_name("value")
                if name:
                    if typ is not None and _text(typ) != "var":
                        f.hints.append(TypeHint(scope, name, _type_text(typ), "annotation"))
                    f.value_hint(scope, name, value)
                if value is not None:
                    stack.append((value, scope, prefix, klass, anon))
            continue

        if kind == "enhanced_for_statement":
            typ, name = node.child_by_field_name("type"), node.child_by_field_name("name")
            if typ is not None and name is not None and _text(typ) != "var":
                f.hints.append(TypeHint(scope, _text(name), _type_text(typ), "annotation"))

        elif kind == "instanceof_expression":        # `o instanceof Adapter ad`: a new name, typed
            typ, name = node.child_by_field_name("right"), node.child_by_field_name("name")
            if typ is not None and name is not None:
                f.hints.append(TypeHint(scope, _text(name), _type_text(typ), "annotation"))

        elif kind == "lambda_expression":            # `x -> x.save()`: `x` hides a field called x
            params = node.child_by_field_name("parameters")
            for p in ([params] if params is not None and params.type == "identifier"
                      else params.named_children if params is not None else []):
                if p.type == "identifier":
                    f.hints.append(TypeHint(scope, _text(p), "", "annotation"))
                elif p.type == "formal_parameter" and (pname := p.child_by_field_name("name")) is not None:
                    f.hints.append(TypeHint(scope, _text(pname), _type_text(p.child_by_field_name("type")),
                                            "annotation"))

        elif kind == "assignment_expression":
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if left is not None and left.type == "field_access" and klass and not anon:
                obj = left.child_by_field_name("object")
                if obj is not None and obj.type == "this":
                    f.value_hint(klass, f"this.{_text(left.child_by_field_name('field'))}", right, origin=scope)

        elif kind == "method_invocation":
            # the line of the called NAME: in a chain over several lines
            # (`a\n.b()\n.c()`) every call starts where the chain does
            name, obj = node.child_by_field_name("name"), node.child_by_field_name("object")
            if name is not None:
                callee = _text(name)
                if obj is None and callee in anon:
                    f.calls.append(CallSite(scope, _line(name), callee, "this", None))
                elif obj is None:
                    f.calls.append(CallSite(scope, _line(name), callee, None, klass))
                else:
                    f.calls.append(CallSite(scope, _line(name), callee, _flat(obj), None if anon else klass))

        elif kind == "object_creation_expression":
            typ = node.child_by_field_name("type")
            simple, qualifier = _simple_type(typ)
            if simple:
                f.calls.append(CallSite(scope, _line(node), simple, qualifier, None if anon else klass))
            f.refs |= _names_in(typ)
            body = next((c for c in node.named_children if c.type == "class_body"), None)
            for child in reversed(node.named_children):
                if child is body:
                    names = frozenset(_text(m.child_by_field_name("name")) for m in body.named_children
                                      if m.type == "method_declaration")
                    stack.append((body, scope, prefix, klass, names))
                elif child is not typ:
                    stack.append((child, scope, prefix, klass, anon))
            continue

        for child in reversed(node.children):
            stack.append((child, scope, prefix, klass, anon))

    return FileFacts(path=path, lang="jvm", degraded=root.has_error, definitions=list(f.defs.values()),
                     imports=f.imports, calls=f.calls, hints=f.hints, exports=[], refs=sorted(f.refs),
                     package=f.package)
