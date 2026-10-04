"""Kotlin facts from tree-sitter (CODE-GRAPH D3).

Same contract as the other extractors (see model), and the same JVM
conventions as extract_java -- one namespace of fully qualified names, so a
Kotlin call into Java and a Java call into Kotlin resolve alike. What Kotlin
adds:
* top-level functions and properties: `<path>::fn`, fully qualified
  `<package>.fn`; Java sees them as static members of the file's FACADE
  class (`FooKt` for `foo.kt`, or `@file:JvmName("X")`), recorded in `exports`;
* an extension function is a function whose `bases` holds its receiver type
  (`fun String.slug()` -> bases ["String"]);
* `object Registry` and `companion object` are definitions of kind `object`;
  an unnamed companion is `Companion`, as the compiler names it;
* the primary constructor is `<Class>.constructor` -- the one definition every
  constructor of the class merges into -- and a `val`/`var` parameter is also
  a property (`this.<name>`);
* `Foo()` is a call of Foo: the resolver tells a constructor from a function.

An object expression (`object : Runnable { ... }`) is anonymous, handled as
extract_java handles an anonymous class. A property read (`user.name`) is not
a call; its bean spellings (`getName`, `isName`, `setName`) are recorded as
mentioned names, so a Java getter used only through Kotlin property syntax is
never called dead.
"""
from __future__ import annotations

import os
import re
from typing import NamedTuple

import tree_sitter as ts
import tree_sitter_kotlin as tsk

from .model import RETURN, CallSite, Definition, FileFacts, ImportBinding, TypeHint

_PARSER = ts.Parser(ts.Language(tsk.language()))
_SIG_MAX = 200
_TYPES = {"user_type", "nullable_type", "function_type", "parenthesized_type", "dynamic_type",
          "non_nullable_type"}
_BODIES = {"class_body", "enum_class_body", "function_body", "block"}
_IDENT_STRING = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_NO_ANON: frozenset[str] = frozenset()
_BOM = "﻿".encode()


def _text(node: ts.Node | None) -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


def _flat(node: ts.Node | None) -> str:
    return " ".join(_text(node).split())


def _line(node: ts.Node) -> int:
    return node.start_point[0] + 1


def _child(node: ts.Node, *types: str) -> ts.Node | None:
    return next((c for c in node.named_children if c.type in types), None)


def _names_in(node: ts.Node | None) -> set[str]:
    out: set[str] = set()
    todo = [node] if node is not None else []
    while todo:
        n = todo.pop()
        if n.type == "identifier":
            out.add(_text(n))
        todo.extend(n.children)
    return out


def _signature(node: ts.Node, src: bytes) -> str:
    body = next((c for c in node.children if c.type in _BODIES), None)
    end = body.start_byte if body is not None else node.end_byte
    return " ".join(src[node.start_byte:end].decode("utf-8", "replace").split())[:_SIG_MAX]


def _doc1(node: ts.Node) -> str | None:
    """The first line of a `/** ... */` (KDoc) block right above the definition."""
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


def _annotations(node: ts.Node | None) -> list[str]:
    mods = _child(node, "modifiers") if node is not None else None
    if mods is None:
        return []
    return [_text(a)[1:].strip() for a in mods.named_children if a.type == "annotation"]


def _modifier_words(node: ts.Node) -> set[str]:
    mods = _child(node, "modifiers")
    return {_text(m) for m in mods.named_children if m.type != "annotation"} if mods is not None else set()


def _modifier_names(node: ts.Node) -> set[str]:
    return _names_in(_child(node, "modifiers"))


def _type_text(node: ts.Node | None) -> str:
    return _flat(node)


def _receiver_text(node: ts.Node) -> str:
    """A call's receiver as the resolver reads it: `a?.b` and `a!!.b` are
    `a.b`, `super@Outer` is `super`; `this@Outer` stays, naming its class."""
    text = _flat(node).replace("?.", ".").replace("!!", "")
    return re.sub(r"\bsuper@\w+", "super", text)


def _is_reference(nav: ts.Node) -> bool:
    """`Type::member` parses as a navigation: a reference, not a call."""
    return any(c.type == "::" for c in nav.children)


# Scope functions. In `x.let { it.go() }` (also, takeIf, takeUnless, use) the
# lambda's parameter IS x, so a call on it is recorded on x. In
# `x.apply { go() }` (run, with(x)) x is the lambda's receiver: `this`, and
# the first place a bare call is looked up (CallSite.implicit).
_IT_SCOPE = frozenset({"let", "also", "takeIf", "takeUnless", "use"})
_THIS_SCOPE = frozenset({"apply", "run"})


class _Ctx(NamedTuple):
    subst: tuple[tuple[str, str], ...] = ()      # (name, the expression it stands for), innermost last
    implicit: str | None = None                  # the enclosing receiver lambda's receiver
    bind: tuple[str, str] | None = None          # what the NEXT lambda binds: ("it" | "this", receiver)


_NO_CTX = _Ctx()


def _substitute(text: str | None, subst: tuple[tuple[str, str], ...]) -> str | None:
    """`it.pause()` inside `op.let { ... }` is `op.pause()`."""
    if not text or not subst or text.startswith("this@"):
        return text
    m = re.match(r"[A-Za-z_]\w*", text)
    if m:
        for name, replacement in reversed(subst):
            if name == m.group():
                return replacement + text[m.end():]
    return text


def facade_name(path: str, jvm_name: str | None) -> str:
    """The class Java calls a Kotlin file's top-level functions through."""
    if jvm_name:
        return jvm_name
    stem = re.sub(r"\W", "_", os.path.splitext(os.path.basename(path))[0])      # as kotlinc: `my-utils` -> My_utils
    return (stem[:1].upper() + stem[1:]) + "Kt"


class _Facts:
    def __init__(self, path: str, src: bytes):
        self.path, self.src = path, src
        self.module = f"{path}::<module>"
        self.package: str | None = None
        self.jvm_name: str | None = None
        self.top_level = False
        self.defs: dict[str, Definition] = {}
        self.imports: list[ImportBinding] = []
        self.calls: list[CallSite] = []
        self.hints: list[TypeHint] = []
        self.refs: set[str] = set()

    def define(self, node: ts.Node, name: str, kind: str, prefix: str | None, *,
               returns: str | None = None, bases: list[str] | None = None,
               decorators: list[str] | None = None) -> str:
        qname = f"{prefix}.{name}" if prefix else f"{self.path}::{name}"
        span = (_line(node), node.end_point[0] + 1)
        self.refs |= _modifier_names(node)
        found = _annotations(node) if decorators is None else decorators
        if qname in self.defs:                    # overloads, secondary constructors
            d = self.defs[qname]
            d.spans.append(span)
            d.decorators.extend(x for x in found if x not in d.decorators)
            if returns and not d.returns:
                d.returns = returns
        else:
            self.defs[qname] = Definition(
                qname=qname, name=name, kind=kind, parent=prefix, spans=[span],
                signature=_signature(node, self.src), doc1=_doc1(node), decorators=list(found),
                exported=False, bases=bases or [], returns=returns)
        return qname

    def params(self, holder: ts.Node | None, scope: str) -> None:
        for p in holder.named_children if holder is not None else []:
            if p.type != "parameter":
                continue
            name, typ = _child(p, "identifier"), _child(p, *_TYPES)
            if name is not None and typ is not None:
                self.hints.append(TypeHint(scope, _text(name), _type_text(typ), "annotation"))
                self.refs |= _names_in(typ)

    def value_hint(self, scope: str, target: str, value: ts.Node | None, origin: str | None = None,
                   subst: tuple[tuple[str, str], ...] = ()) -> None:
        while value is not None and value.type in ("parenthesized_expression", "as_expression"):
            if value.type == "as_expression":
                right = value.child_by_field_name("right")
                if right is not None:
                    self.hints.append(TypeHint(scope, target, _type_text(right), "annotation", origin))
                return
            value = value.named_children[0] if value.named_children else None
        if value is None:
            return
        if value.type == "call_expression":
            callee = value.named_children[0] if value.named_children else None
            if callee is not None and (callee.type == "identifier" or (
                    callee.type == "navigation_expression" and not _is_reference(callee)
                    and "(" not in _text(callee.named_children[0] if callee.named_children else None))):
                # `Foo()` constructs, `Foo.create()` returns: the resolver tells which
                self.hints.append(TypeHint(scope, target, _substitute(_receiver_text(callee), subst), "call",
                                           origin))
        elif value.type in ("identifier", "navigation_expression", "this_expression") and not (
                value.type == "navigation_expression" and _is_reference(value)):
            self.hints.append(TypeHint(scope, target, _substitute(_receiver_text(value), subst), "alias", origin))


def _class_kind(node: ts.Node) -> str:
    if any(c.type == "interface" for c in node.children):
        return "interface"
    words = _modifier_words(node)
    if "enum" in words:
        return "enum"
    if "annotation" in words:
        return "interface"
    return "class"


def _bases(node: ts.Node) -> list[str]:
    out: list[str] = []
    specs = _child(node, "delegation_specifiers")
    for spec in specs.named_children if specs is not None else []:
        target = spec.named_children[0] if spec.named_children else None
        if target is not None and target.type in ("constructor_invocation", "explicit_delegation"):
            target = _child(target, *_TYPES)
        if target is not None:
            out.append(_type_text(target))
    return out


def _function_parts(node: ts.Node) -> tuple[ts.Node | None, ts.Node | None, ts.Node | None]:
    """(extension receiver type, parameters, return type) of a function_declaration."""
    receiver = params = returns = None
    seen_name = False
    for i, c in enumerate(node.children):
        if node.field_name_for_child(i) == "name":
            seen_name = True
        elif c.type == "function_value_parameters":
            params = c
        elif c.type in _TYPES:
            if not seen_name:
                receiver = c
            elif params is not None and returns is None:
                returns = c
    return receiver, params, returns


def _initializer(node: ts.Node) -> ts.Node | None:
    """The expression after `=` in a property declaration."""
    after = False
    for c in node.children:
        if after and c.is_named:
            return c
        if c.type == "=":
            after = True
    return None


def extract(path: str, src: bytes) -> FileFacts:
    src = src.removeprefix(_BOM)                     # a byte-order mark would read as a syntax error
    root = _PARSER.parse(src).root_node
    f = _Facts(path, src)
    # (node, caller scope, definition prefix, enclosing class, names an enclosing object expression
    # declares, the enclosing lambdas' context)
    stack: list[tuple[ts.Node, str, str | None, str | None, frozenset[str], _Ctx]] = [
        (root, f.module, None, None, _NO_ANON, _NO_CTX)]
    while stack:
        node, scope, prefix, klass, anon, ctx = stack.pop()
        kind = node.type
        if kind == "identifier":
            text = _text(node)
            f.refs.add(text)
            if text[:1].islower():               # Kotlin reads and writes a Java bean as a property
                cap = text[:1].upper() + text[1:]
                f.refs |= {f"get{cap}", f"set{cap}", f"is{cap}"}
                if text.startswith("is") and text[2:3].isupper():
                    f.refs.add("set" + text[2:])  # `isFoo = true` is `setFoo(true)`
            continue
        if kind == "string_content" and _IDENT_STRING.match(text := _text(node)):
            f.refs.add(text)
            continue
        if kind in ("line_comment", "block_comment"):
            continue

        if kind == "file_annotation":
            inv = _child(node, "constructor_invocation")
            if inv is not None and _flat(_child(inv, "user_type")) == "JvmName":
                content = list(inv.named_children[-1].named_children) if inv.named_children else []
                strings = [_text(s) for arg in content for s in [_child(arg, "string_literal")] if s is not None]
                if strings:
                    f.jvm_name = strings[0].strip('"') or None
            continue

        if kind == "package_header":
            f.package = _text(_child(node, "qualified_identifier", "identifier")) or None
            continue

        if kind == "import":
            dotted = _text(_child(node, "qualified_identifier", "identifier"))
            alias = [c for c in node.named_children if c.type == "identifier"]
            star = any(c.type == "*" for c in node.children) or _text(node).rstrip().endswith("*")
            if star:
                f.imports.append(ImportBinding(f.module, "*", dotted, 0, "*"))
            elif dotted:
                module, _, attr = dotted.rpartition(".")
                f.imports.append(ImportBinding(f.module, _text(alias[-1]) if alias else attr, module, 0, attr))
            continue

        if kind in ("class_declaration", "object_declaration", "companion_object"):
            name_node = node.child_by_field_name("name")
            name = _text(name_node) or ("Companion" if kind == "companion_object" else "")
            if not name:
                continue
            ctor = _child(node, "primary_constructor")
            decorators = _annotations(node)
            qname = f.define(node, name, "object" if kind != "class_declaration" else _class_kind(node),
                             prefix, bases=_bases(node), decorators=decorators)
            f.refs |= _names_in(_child(node, "delegation_specifiers"))
            if ctor is not None:
                c_q = f.define(ctor, "constructor", "method", qname, decorators=_annotations(ctor))
                for p in (_child(ctor, "class_parameters") or ctor).named_children:
                    if p.type != "class_parameter":
                        continue
                    pname, ptype = _child(p, "identifier"), _child(p, *_TYPES)
                    if pname is None or ptype is None:
                        continue
                    f.refs |= _names_in(ptype) | _modifier_names(p)
                    f.hints.append(TypeHint(c_q, _text(pname), _type_text(ptype), "annotation"))
                    f.hints.append(TypeHint(qname, _text(pname), _type_text(ptype), "annotation"))
                    if any(c.type in ("val", "var") for c in p.children):
                        f.hints.append(TypeHint(qname, f"this.{_text(pname)}", _type_text(ptype), "annotation"))
                # a default value may call something: it runs in the constructor
                for p in (_child(ctor, "class_parameters") or ctor).named_children:
                    default = _initializer(p) if p.type == "class_parameter" else None
                    if default is not None:
                        stack.append((default, c_q, qname, qname, _NO_ANON, _NO_CTX))
            specs = _child(node, "delegation_specifiers")
            for spec in specs.named_children if specs is not None else []:
                args = [a for c in spec.named_children if c.type in ("constructor_invocation", "explicit_delegation")
                        for a in c.named_children if a.type not in _TYPES]
                for a in args:                    # `: Base(make())`, `: I by impl()` -- evaluated in the class
                    stack.append((a, qname, qname, qname, _NO_ANON, _NO_CTX))
            body = _child(node, "class_body", "enum_class_body")
            if body is not None:
                for entry in body.named_children if body.type == "enum_class_body" else []:
                    if entry.type == "enum_entry" and (ename := _text(_child(entry, "identifier"))):
                        f.hints.append(TypeHint(qname, f"this.{ename}", name, "annotation"))
                stack.append((body, qname, qname, qname, _NO_ANON, _NO_CTX))
            continue

        if kind == "object_literal":
            f.refs |= _names_in(_child(node, "delegation_specifiers"))
            body = _child(node, "class_body")
            if body is not None:
                names = frozenset(_text(m.child_by_field_name("name")) for m in body.named_children
                                  if m.type == "function_declaration")
                # a new `this`; the enclosing lambdas' parameters are still in reach
                inner = _Ctx(tuple((n, r) for n, r in ctx.subst if n != "this"))
                stack.append((body, scope, prefix, klass, names, inner))
            continue

        if kind == "function_declaration":
            name = _text(node.child_by_field_name("name"))
            receiver, params, ret = _function_parts(node)
            f.refs |= _names_in(receiver) | _names_in(ret)
            body = _child(node, "function_body")
            if anon or not name:                  # a member of an object expression: not a definition
                f.refs |= _modifier_names(node)
                f.params(params, scope)
                if body is not None:
                    stack.append((body, scope, prefix, klass, anon, ctx._replace(bind=None)))
                continue
            member = prefix is not None and prefix in f.defs and f.defs[prefix].kind in (
                "class", "interface", "enum", "object")
            if prefix is None:
                f.top_level = True
            qname = f.define(node, name, "method" if member else "function", prefix,
                             returns=_type_text(ret) or None,
                             bases=[_type_text(receiver)] if receiver is not None else None)
            f.params(params, qname)
            if receiver is not None:              # `this` inside an extension is the receiver
                f.hints.append(TypeHint(qname, "this", _type_text(receiver), "annotation"))
            if body is not None and ret is None and any(c.type == "=" for c in body.children):
                # `fun create() = Foo()`: no declared type, the body is the return value
                f.value_hint(qname, RETURN, _initializer(body), subst=ctx.subst)
            if body is not None:                  # a local function still sees the lambdas around it
                stack.append((body, qname, qname, klass, _NO_ANON, ctx._replace(bind=None)))
            continue

        if kind == "secondary_constructor":
            if klass is None or anon:
                for child in reversed(node.children):
                    stack.append((child, scope, prefix, klass, anon, ctx))
                continue
            qname = f.define(node, "constructor", "method", klass)
            f.params(_child(node, "function_value_parameters"), qname)
            for child in reversed(node.named_children):
                if child.type in ("constructor_delegation_call", "block"):
                    stack.append((child, qname, qname, klass, _NO_ANON, _NO_CTX))
            continue

        if kind == "property_declaration":
            var = _child(node, "variable_declaration")
            value = _initializer(node)
            f.refs |= _modifier_names(node)
            if prefix is None:
                f.top_level = True
            if var is not None:
                name_node, typ = _child(var, "identifier"), _child(var, *_TYPES)
                name = _text(name_node)
                f.refs |= _names_in(typ)
                in_type = klass is not None and prefix == klass and not anon
                target, where = (f"this.{name}", klass) if in_type else (name, scope)
                if name and where is not None:
                    if typ is not None:
                        f.hints.append(TypeHint(where, target, _type_text(typ), "annotation"))
                    f.value_hint(where, target, value, origin=scope if in_type else None, subst=ctx.subst)
            for child in reversed(node.named_children):
                if child.type not in ("variable_declaration", "modifiers", "multi_variable_declaration"):
                    stack.append((child, scope, prefix, klass, anon, ctx))
            continue

        if kind == "lambda_literal":
            lp = _child(node, "lambda_parameters")
            names: list[str] = []
            for v in lp.named_children if lp is not None else []:
                name, typ = _child(v, "identifier"), _child(v, *_TYPES)
                if name is not None:
                    names.append(_text(name))
                    # untyped too: a lambda's parameter hides a property of its name
                    f.hints.append(TypeHint(scope, _text(name), _type_text(typ), "annotation"))
            params = names or ["it"]              # a parameter shadows every outer binding of its name
            subst = tuple((n, r) for n, r in ctx.subst if n not in params)
            implicit = ctx.implicit
            if ctx.bind and ctx.bind[0] == "it":
                subst += ((params[0], ctx.bind[1]),)
            elif ctx.bind:
                implicit = ctx.bind[1]
                subst = tuple((n, r) for n, r in subst if n != "this") + (("this", ctx.bind[1]),)
            inner = _Ctx(subst, implicit)
            for child in reversed(node.children):
                stack.append((child, scope, prefix, klass, anon, inner))
            continue

        if kind == "for_statement":
            var = _child(node, "variable_declaration")
            if var is not None and _child(var, *_TYPES) is not None:
                f.hints.append(TypeHint(scope, _text(_child(var, "identifier")),
                                        _type_text(_child(var, *_TYPES)), "annotation"))

        elif kind == "assignment":
            target = node.named_children[0] if node.named_children else None
            if (target is not None and target.type == "navigation_expression" and klass and not anon
                    and target.named_children and target.named_children[0].type == "this_expression"):
                member = target.named_children[-1]
                value = node.named_children[-1] if len(node.named_children) > 1 else None
                f.value_hint(klass, f"this.{_text(member)}", value, origin=scope, subst=ctx.subst)

        elif kind == "call_expression":
            callee = node.named_children[0] if node.named_children else None
            lam = _child(node, "annotated_lambda")
            bind: tuple[str, str] | None = None
            if callee is not None and callee.type == "identifier":
                name = _text(callee)
                if name in anon:
                    f.calls.append(CallSite(scope, _line(node), name, "this", None))
                else:
                    f.calls.append(CallSite(scope, _line(node), name, None, klass, ctx.implicit))
            elif (callee is not None and callee.type == "call_expression" and lam is not None
                    and _text(callee.named_children[0] if callee.named_children else None) == "with"):
                # `with(x) { ... }` parses as a call of the call `with(x)`: its lambda is this one
                args = _child(callee, "value_arguments")
                first = args.named_children[0] if args is not None and args.named_children else None
                if first is not None and first.type == "value_argument" and first.named_children:
                    first = first.named_children[0]
                if first is not None:
                    bind = ("this", _substitute(_receiver_text(first), ctx.subst))
            elif (callee is not None and callee.type == "navigation_expression" and not _is_reference(callee)
                    and len(callee.named_children) >= 2 and callee.named_children[-1].type == "identifier"):
                recv = _substitute(_receiver_text(callee.named_children[0]), ctx.subst)
                name = _text(callee.named_children[-1])
                # the line of the called NAME, not of the chain it ends (`a\n.b()\n.c()`)
                f.calls.append(CallSite(scope, _line(callee.named_children[-1]), name, recv, None if anon else klass))
                if lam is not None and name in _IT_SCOPE:
                    bind = ("it", recv)
                elif lam is not None and name in _THIS_SCOPE:
                    bind = ("this", recv)
            for child in reversed(node.children):
                stack.append((child, scope, prefix, klass, anon,
                              ctx._replace(bind=bind) if child == lam else ctx._replace(bind=None)))
            continue

        for child in reversed(node.children):
            stack.append((child, scope, prefix, klass, anon, ctx))

    exports = [ImportBinding(f.module, facade_name(path, f.jvm_name), "", 0, None)] if f.top_level else []
    return FileFacts(path=path, lang="jvm", degraded=root.has_error, definitions=list(f.defs.values()),
                     imports=f.imports, calls=f.calls, hints=f.hints, exports=exports, refs=sorted(f.refs),
                     package=f.package)
