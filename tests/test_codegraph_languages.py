"""The language table is the one place a family is named (CODE-GRAPH D3).

Its kinds are literal copies of each resolver's own constants -- the query
path must classify a row without loading a resolver -- so every copy is held
equal to its source here. Deleting a family's constant, or changing it on one
side only, turns a test below red.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from singularity.codegraph import languages, resolve_jvm, resolve_py, resolve_ts, scope


def test_each_family_copies_its_resolvers_kinds_exactly():
    assert languages.family("python").containers == resolve_py.CONTAINERS
    assert languages.family("python").never_guessed == resolve_py.NEVER_GUESSED
    assert languages.family("ts").containers == resolve_ts._TYPELIKE
    assert languages.family("ts").never_guessed == resolve_ts.NEVER_GUESSED
    assert languages.family("jvm").containers == resolve_jvm.CONTAINERS
    assert languages.family("jvm").never_guessed == resolve_jvm.NEVER_GUESSED


def test_only_the_jvm_family_resolves_through_packages():
    """A file entering or leaving a JVM package re-resolves whoever listed it
    (update); a Python or TypeScript file has no such membership."""
    assert languages.membership("jvm", "com.ex") == {languages.PACKAGE_DEP + "com.ex"}
    assert languages.membership("jvm", None) == {languages.PACKAGE_DEP}
    assert languages.membership("python", None) == languages.membership("ts", "x") == set()


def test_every_family_names_a_resolver_that_exists():
    for fam in languages.FAMILIES:
        module, _, cls = fam.resolver.partition(":")
        assert hasattr(__import__(f"singularity.codegraph.{module}", fromlist=[cls]), cls), fam.lang


def test_the_table_reads_exactly_the_graphs_universe():
    """A suffix the universe admits but no family reads would reach `extract`
    as a KeyError mid-build; one a family reads but the universe never admits
    is dead weight. The two lists are one list."""
    assert languages.SUFFIXES == scope.CODE_SUFFIXES


_SOURCE = {"python": b"def f():\n    pass\n", "ts": b"function f() {}\n", "jvm": b"class F {}\n"}


@pytest.mark.parametrize("path, lang", [("a/b.py", "python"), ("x.tsx", "ts"), ("x.js", "ts"),
                                        ("x.jsx", "ts"), ("x.ts", "ts"), ("LEGACY.PY", "python"),
                                        ("a/F.java", "jvm"), ("a/F.kt", "jvm")])
def test_a_file_goes_to_its_family_by_suffix_in_any_case(path, lang):
    assert languages.lang_of(path) == lang
    got, facts = languages.extract(path, _SOURCE[lang])
    assert got == lang and facts.lang == lang


def test_a_suffix_no_family_reads_is_refused():
    assert languages.lang_of("notes.md") is None
    with pytest.raises(KeyError):
        languages.extract("notes.md", b"# x\n")


def test_the_table_is_parser_free():
    """`rows`, `update` and `analysis` read it on the query path."""
    probe = ("import sys\nimport singularity.codegraph.languages\n"
             "print(sorted(m for m in sys.modules if m.startswith('tree_sitter')))\n")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"
