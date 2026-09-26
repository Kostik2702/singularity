"""Python facts from tree-sitter (CODE-GRAPH D-1).

Why tree-sitter and not `ast`: `ast` parses the grammar of the Python that
RUNS the engine (3.10 here), fails on a 3.12 f-string or a PEP 695 alias, and
fails the whole file on one syntax error. tree-sitter recovers: a broken
`def` yields the definitions around it and the file is marked degraded, so
the graph loses one region, not one file.

Walked with an explicit stack, not recursion: generated code nests deep
enough to reach Python's recursion limit, and a limit hit in a worker kills
the file silently.
"""
from __future__ import annotations

import tree_sitter as ts
import tree_sitter_python as tsp

from .model import CallSite, Definition, FileFacts, ImportBinding, TypeHint

_LANGUAGE = ts.Language(tsp.language())
_PARSER = ts.Parser(_LANGUAGE)
_SIG_MAX = 200


def _text(node: ts.Node | None) -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


def _line(node: ts.Node) -> int:
    return node.start_point[0] + 1


def _doc1(body: ts.Node | None) -> str | None:
    if body is None:
        return None
    for stmt in body.named_children:
        if stmt.type == "comment":
            continue
        if stmt.type == "expression_statement" and stmt.named_children \
                and stmt.named_children[0].type == "string":
            raw = _text(stmt.named_children[0]).strip("rRbBuUfF")
            for quote in ('"""', "'''", '"', "'"):
                if raw.startswith(quote) and raw.endswith(quote) and len(raw) >= 2 * len(quote):
                    raw = raw[len(quote):-len(quote)]
                    break
            for line in raw.splitlines():
                if line.strip():
                    return line.strip()[:_SIG_MAX]
        return None
    return None


def _signature(node: ts.Node, src: bytes) -> str:
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    head = src[node.start_byte:end].decode("utf-8", "replace")
    return " ".join(head.split()).rstrip(":")[:_SIG_MAX]


def _decorators(node: ts.Node) -> list[str]:
    parent = node.parent
    if parent is None or parent.type != "decorated_definition":
        return []
    return [_text(c)[1:].strip() for c in parent.children if c.type == "decorator"]


def _import_bindings(node: ts.Node, scope: str) -> list[ImportBinding]:
    out: list[ImportBinding] = []
    if node.type == "import_statement":
        for child in node.named_children:
            if child.type == "dotted_name":
                name = _text(child)
                out.append(ImportBinding(scope, name, name, 0, None))
            elif child.type == "aliased_import":
                name = _text(child.child_by_field_name("name"))
                out.append(ImportBinding(scope, _text(child.child_by_field_name("alias")), name, 0, None))
        return out
    module_node = node.child_by_field_name("module_name")
    level, module = 0, ""
    if module_node is not None and module_node.type == "relative_import":
        for part in module_node.children:
            if part.type == "import_prefix":
                level = _text(part).count(".")
            elif part.type == "dotted_name":
                module = _text(part)
    else:
        module = _text(module_node)
    for child in node.children_by_field_name("name"):
        if child.type == "dotted_name":
            name = _text(child)
            out.append(ImportBinding(scope, name, module, level, name))
        elif child.type == "aliased_import":
            name = _text(child.child_by_field_name("name"))
            out.append(ImportBinding(scope, _text(child.child_by_field_name("alias")), module, level, name))
    if any(c.type == "wildcard_import" for c in node.children):
        out.append(ImportBinding(scope, "*", module, level, "*"))
    return out


def _params(fn: ts.Node, qname: str) -> list[TypeHint]:
    params = fn.child_by_field_name("parameters")
    out: list[TypeHint] = []
    if params is None:
        return out
    for p in params.named_children:
        if p.type in ("typed_parameter", "typed_default_parameter"):
            ann = p.child_by_field_name("type")
            name_node = p.child_by_field_name("name")
            if name_node is None:
                name_node = next((c for c in p.named_children if c.type == "identifier"), None)
            if ann is not None and name_node is not None:
                out.append(TypeHint(qname, _text(name_node), _text(ann), "annotation"))
    return out


def extract(path: str, src: bytes) -> FileFacts:
    tree = _PARSER.parse(src)
    root = tree.root_node
    module_scope = f"{path}::<module>"
    defs: dict[str, Definition] = {}
    imports: list[ImportBinding] = []
    calls: list[CallSite] = []
    hints: list[TypeHint] = []

    # (node, scope qname used as caller, definition qname prefix or None, enclosing class qname)
    stack: list[tuple[ts.Node, str, str | None, str | None]] = [(root, module_scope, None, None)]
    while stack:
        node, scope, prefix, klass = stack.pop()
        kind = node.type
        if kind in ("function_definition", "class_definition"):
            name = _text(node.child_by_field_name("name"))
            qname = f"{prefix}.{name}" if prefix else f"{path}::{name}"
            parent_is_class = klass is not None and prefix == klass
            dkind = "class" if kind == "class_definition" else ("method" if parent_is_class else "function")
            span = (_line(node), node.end_point[0] + 1)
            if qname in defs:                     # property/setter, @overload, if/else defs
                defs[qname].spans.append(span)
            else:
                bases = []
                if kind == "class_definition":
                    sup = node.child_by_field_name("superclasses")
                    if sup is not None:
                        bases = [_text(b) for b in sup.named_children if b.type != "keyword_argument"]
                defs[qname] = Definition(
                    qname=qname, name=name, kind=dkind, parent=prefix, spans=[span],
                    signature=_signature(node, src), doc1=_doc1(node.child_by_field_name("body")),
                    decorators=_decorators(node), exported=prefix is None and not name.startswith("_"),
                    bases=bases)
            if kind == "function_definition":
                hints.extend(_params(node, qname))
            body = node.child_by_field_name("body")
            child_class = qname if kind == "class_definition" else klass
            for child in reversed(node.children):
                # `==`, not `is`: py-tree-sitter hands out a fresh Node object on
                # every access, so identity never holds and the body is skipped.
                if body is not None and child == body:
                    stack.append((child, qname, qname, child_class))
                elif child.type in ("parameters", "argument_list"):
                    # defaults and base-class expressions run in the ENCLOSING scope
                    stack.append((child, scope, prefix, klass))
            continue
        if kind in ("import_statement", "import_from_statement"):
            imports.extend(_import_bindings(node, scope))
            continue
        if kind == "call":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type == "identifier":
                calls.append(CallSite(scope, _line(node), _text(fn), None, klass))
            elif fn is not None and fn.type == "attribute":
                attr = fn.child_by_field_name("attribute")
                obj = fn.child_by_field_name("object")
                calls.append(CallSite(scope, _line(node), _text(attr),
                                      " ".join(_text(obj).split()), klass))
        elif kind == "assignment":
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            ann = node.child_by_field_name("type")
            target = " ".join(_text(left).split()) if left is not None else ""
            if left is not None and left.type in ("identifier", "attribute"):
                hint_scope = klass if target.startswith("self.") and klass else scope
                if ann is not None:
                    hints.append(TypeHint(hint_scope, target, _text(ann), "annotation"))
                if right is not None and right.type == "call":
                    ctor = right.child_by_field_name("function")
                    if ctor is not None and ctor.type in ("identifier", "attribute"):
                        hints.append(TypeHint(hint_scope, target, _text(ctor), "ctor"))
        for child in reversed(node.children):
            stack.append((child, scope, prefix, klass))

    return FileFacts(path=path, lang="python", degraded=root.has_error,
                     definitions=list(defs.values()), imports=imports, calls=calls, hints=hints)
