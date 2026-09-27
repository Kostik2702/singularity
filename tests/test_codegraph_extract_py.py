"""Python facts from tree-sitter: what the resolver will stand on (CODE-GRAPH P1)."""
from __future__ import annotations

from singularity.codegraph.extract_py import extract

SRC = b'''"""Module doc."""
import os
import numpy as np
from . import sibling
from ..pkg.mod import helper as h, other
from .star import *

TOP = build_top()


class Store(Base, metaclass=Meta):
    """Keeps things."""

    @property
    def size(self):
        return self._size

    @size.setter
    def size(self, value):
        self._validate(value)

    def load(self, cfg: Config, n: int = 3):
        self.repo = Repo()
        cache: Cache = make_cache()
        local = Parser()
        local.feed(np.zeros(3))

        def inner():
            return self.load(None)
        return inner()


@decorator("x")
def run():
    h(os.path.join("a", "b"))
'''


def _facts():
    return extract("pkg/store.py", SRC)


def test_definitions_carry_their_scope_in_the_qname():
    by_q = {d.qname: d for d in _facts().definitions}
    assert by_q["pkg/store.py::Store"].kind == "class"
    assert by_q["pkg/store.py::Store"].bases == ["Base"]          # metaclass= is not a base
    assert by_q["pkg/store.py::Store.load"].kind == "method"
    assert by_q["pkg/store.py::Store.load.inner"].kind == "function"
    assert by_q["pkg/store.py::run"].decorators == ['decorator("x")']
    assert by_q["pkg/store.py::Store"].doc1 == "Keeps things."
    assert by_q["pkg/store.py::run"].exported and not by_q["pkg/store.py::Store.load"].exported


def test_a_legal_redefinition_merges_into_one_node_with_two_spans():
    """property + setter share a name in one class body. Two nodes would
    violate qname uniqueness; replacing one with the other would lose the
    setter body's calls (CODE-GRAPH amendment A4)."""
    sizes = [d for d in _facts().definitions if d.qname == "pkg/store.py::Store.size"]
    assert len(sizes) == 1 and len(sizes[0].spans) == 2
    callers = {c.caller for c in _facts().calls if c.callee == "_validate"}
    assert callers == {"pkg/store.py::Store.size"}


def test_imports_keep_level_alias_and_attribute():
    imp = {(i.local, i.module, i.level, i.attr) for i in _facts().imports}
    assert ("os", "os", 0, None) in imp
    assert ("np", "numpy", 0, None) in imp
    assert ("sibling", "", 1, "sibling") in imp
    assert ("h", "pkg.mod", 2, "helper") in imp
    assert ("other", "pkg.mod", 2, "other") in imp
    assert ("*", "star", 1, "*") in imp


def test_calls_record_caller_receiver_and_class():
    calls = {(c.caller, c.callee, c.receiver) for c in _facts().calls}
    assert ("pkg/store.py::<module>", "build_top", None) in calls
    assert ("pkg/store.py::Store.load", "feed", "local") in calls
    assert ("pkg/store.py::Store.load.inner", "load", "self") in calls
    assert ("pkg/store.py::run", "join", "os.path") in calls
    assert ("pkg/store.py::<module>", "decorator", None) in calls   # runs at import time
    inner = next(c for c in _facts().calls if c.caller.endswith("inner"))
    assert inner.klass == "pkg/store.py::Store"                   # `self` in a closure


def test_type_hints_from_constructors_and_annotations():
    hints = {(h.scope, h.target, h.type_expr, h.source) for h in _facts().hints}
    assert ("pkg/store.py::Store", "self.repo", "Repo", "ctor") in hints
    assert ("pkg/store.py::Store.load", "local", "Parser", "ctor") in hints
    assert ("pkg/store.py::Store.load", "cache", "Cache", "annotation") in hints
    assert ("pkg/store.py::Store.load", "cfg", "Config", "annotation") in hints


def test_a_broken_file_is_degraded_not_lost():
    facts = extract("x.py", b"def ok():\n    a()\n\ndef broken(:\n    pass\n\ndef after():\n    b()\n")
    assert facts.degraded
    names = {d.name for d in facts.definitions}
    assert {"ok", "after"} <= names
    assert {"a", "b"} <= {c.callee for c in facts.calls}


def test_syntax_newer_than_the_engines_python_parses():
    """PEP 695 and PEP 701 are 3.12 syntax; `ast` on 3.10 rejects the file."""
    src = b'type Alias[T] = list[T]\n\ndef f[T](x: T) -> T:\n    return g(f"{x!r:{"w"}}")\n'
    facts = extract("new.py", src)
    assert "new.py::f" in {d.qname for d in facts.definitions}
    assert "g" in {c.callee for c in facts.calls}
