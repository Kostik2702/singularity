"""What the Java and Kotlin extractors record (CODE-GRAPH D3): the
conventions the JVM resolver and the yardstick's labels both rely on.
"""
from __future__ import annotations

from singularity.codegraph import extract_java, extract_kotlin
from singularity.codegraph.model import RETURN, decode_facts, encode_facts

JAVA = b"""package com.ex.app;

import java.util.List;
import java.util.*;
import static com.ex.util.Strings.slug;
import static com.ex.util.Math2.*;

/** A screen. More text. */
@Deprecated
public class Screen extends Base<String> implements Runnable, com.x.Iface {
    private final Repo repo = new Repo();
    @Inject Service service;
    public Screen(Repo r) { super(r); this.repo2 = r; }
    Screen() { this(new Repo()); }
    @Override
    public void run() {
        Helper h = Helper.create();
        repo.load(1).save();
        new Runnable() { public void run() { run(); inner(); } };
        int n = compute(2);
    }
    int compute(int n) { return 0; }
    int compute(String s) { return 1; }
    enum Kind { A, B; Kind next() { return A; } }
    record Point(int x, int y) {}
}
@interface Marker {}
"""

KOTLIN = b"""@file:JvmName("ScreenUtils")
package com.ex.app

import com.ex.data.Repo
import com.ex.data.Repo as Store
import com.ex.util.*

/** Top doc. */
fun topLevel(n: Int): String = n.toString()
fun List<String>.second(): String = this[1]
fun make() = Repo()

class Screen @Inject constructor(private val repo: Repo, other: Int) : Base(), Runnable {
    @Inject lateinit var service: Service
    private val helper: Helper = Helper.create()
    constructor(r: Repo) : this(r, 0)
    override fun run() {
        repo.find(1)?.let { it.pause() }
        repo.apply { refresh() }
        with(helper) { go() }
        object : Runnable {
            override fun run() { tick() }
            fun tick() {}
        }
        val n = user.name
    }
    companion object {
        @JvmStatic fun create(): Screen = Screen(Repo())
    }
    object Registry {
        fun add() {}
    }
}
class Factory {
    companion object Maker {
        fun build() = Factory()
    }
}
enum class Kind {
    A, B;
    fun next() = A
}
"""


def _java():
    return extract_java.extract("app/Screen.java", JAVA)


def _kotlin(path="app/Screen.kt", src=KOTLIN):
    return extract_kotlin.extract(path, src)


def _defs(f):
    return {d.qname.split("::")[1]: d for d in f.definitions}


def test_java_package_imports_static_and_wildcard():
    f = _java()
    assert f.lang == "jvm" and f.package == "com.ex.app" and not f.degraded
    got = {(b.local, b.module, b.level, b.attr) for b in f.imports}
    assert got == {("List", "java.util", 0, "List"), ("*", "java.util", 0, "*"),
                   ("slug", "com.ex.util.Strings", 1, "slug"), ("*", "com.ex.util.Math2", 1, "*")}


def test_java_definitions_constructors_overloads_records_and_enums():
    d = _defs(_java())
    assert d["Screen"].bases == ["Base<String>", "Runnable", "com.x.Iface"]
    assert d["Screen"].decorators == ["Deprecated"] and d["Screen"].doc1 == "A screen. More text."
    assert len(d["Screen.constructor"].spans) == 2                  # both constructors, one node
    assert len(d["Screen.compute"].spans) == 2                      # overloads merge
    assert d["Screen.run"].decorators == ["Override"]
    assert {"Screen.Point.x", "Screen.Point.y"} <= set(d)            # record components are accessors
    assert d["Screen.Kind"].kind == "enum" and d["Marker"].kind == "interface"
    assert all(not x.exported for x in d.values())


def test_java_anonymous_class_is_not_a_definition_and_its_own_calls_stay_its_own():
    f = _java()
    assert not any(q.endswith("run.run") for q in _defs(f))
    anon = [c for c in f.calls if c.callee in ("run", "inner") and c.caller.endswith("Screen.run")]
    own = next(c for c in anon if c.callee == "run")
    outer = next(c for c in anon if c.callee == "inner")
    assert (own.receiver, own.klass) == ("this", None)             # the anonymous body declares run()
    assert outer.receiver is None and outer.klass.endswith("::Screen")


def test_java_pattern_variables_are_typed_and_chained_calls_sit_on_their_own_line():
    f = extract_java.extract("A.java", b"class A {\n  void f(Object o) {\n    if (o instanceof Adapter ad) {}\n"
                                       b"    Builder.create()\n        .size(3)\n        .build();\n  }\n}\n")
    hints = {(h.target, h.type_expr) for h in f.hints}
    assert ("ad", "Adapter") in hints
    assert {c.callee: c.line for c in f.calls} == {"create": 4, "size": 5, "build": 6}


def test_kotlin_chained_calls_sit_on_their_own_line_and_is_properties_name_setters():
    f = _kotlin("A.kt", b"package a\nfun f(b: Builder) {\n    b.create()\n        .size(3)\n"
                        b"    b.apply { isEnabled = true }\n}\n")
    assert {c.callee: c.line for c in f.calls if c.callee in ("create", "size")} == {"create": 3, "size": 4}
    assert "setEnabled" in f.refs                                   # `isEnabled = true` calls setEnabled


def test_java_type_hints_fields_locals_params_and_enum_constants():
    hints = {(h.scope.split("::")[1], h.target, h.type_expr, h.source) for h in _java().hints}
    assert ("Screen", "this.repo", "Repo", "annotation") in hints
    assert ("Screen", "this.repo", "Repo", "ctor") in hints
    assert ("Screen", "this.service", "Service", "annotation") in hints
    assert ("Screen.run", "h", "Helper.create", "call") in hints
    assert ("Screen.constructor", "r", "Repo", "annotation") in hints
    assert ("Screen", "this.repo2", "r", "alias") in hints
    assert ("Screen.Kind", "this.A", "Kind", "annotation") in hints


def test_kotlin_facade_top_level_extension_and_expression_body():
    f = _kotlin()
    d = _defs(f)
    assert f.package == "com.ex.app"
    assert [(e.local, e.module) for e in f.exports] == [("ScreenUtils", "")]      # @file:JvmName
    assert d["topLevel"].kind == "function" and d["topLevel"].returns == "String"
    assert d["second"].bases == ["List<String>"]                                  # the extension receiver
    ret = [h for h in f.hints if h.scope.endswith("::make") and h.target == RETURN]
    assert [(h.type_expr, h.source) for h in ret] == [("Repo", "call")]
    plain = _kotlin("app/screen_helpers.kt", b"package a\nfun helper() {}\n")
    assert [e.local for e in plain.exports] == ["Screen_helpersKt"]
    assert _kotlin("app/Only.kt", b"package a\nclass Only\n").exports == []    # no top level, no facade


def test_kotlin_classes_constructors_companions_and_objects():
    d = _defs(_kotlin())
    assert d["Screen"].bases == ["Base", "Runnable"]
    assert len(d["Screen.constructor"].spans) == 2 and d["Screen.constructor"].decorators == ["Inject"]
    assert d["Screen.Companion"].kind == "object" and "Screen.Companion.create" in d
    assert d["Screen.Registry"].kind == "object" and d["Factory.Maker"].kind == "object"
    assert d["Kind"].kind == "enum" and "Kind.next" in d


def test_kotlin_imports_aliases_and_wildcards():
    got = {(b.local, b.module, b.attr) for b in _kotlin().imports}
    assert got == {("Repo", "com.ex.data", "Repo"), ("Store", "com.ex.data", "Repo"),
                   ("*", "com.ex.util", "*")}


def test_kotlin_scope_functions_bind_it_and_this():
    calls = {c.callee: c for c in _kotlin().calls}
    assert calls["pause"].receiver == "repo.find(1)"                 # `it` of `?.let` is the receiver
    assert calls["refresh"].receiver is None and calls["refresh"].implicit == "repo"
    assert calls["go"].implicit == "helper"                          # with(helper) { go() }
    assert calls["tick"].receiver == "this" and calls["tick"].klass is None   # an object expression


def test_kotlin_property_hints_and_bean_spellings():
    f = _kotlin()
    hints = {(h.scope.split("::")[1], h.target, h.type_expr, h.source) for h in f.hints}
    assert ("Screen", "this.repo", "Repo", "annotation") in hints   # a `val` constructor parameter
    assert ("Screen", "other", "Int", "annotation") in hints         # a plain one: initialisers see it
    assert ("Screen", "this.service", "Service", "annotation") in hints
    assert ("Screen", "this.helper", "Helper.create", "call") in hints
    assert {"getName", "isName", "setName"} <= set(f.refs)           # `user.name` may be a Java getter


def test_a_facade_is_named_as_kotlinc_names_it_and_a_bom_is_not_a_syntax_error():
    assert extract_kotlin.facade_name("app/my-utils.kt", None) == "My_utilsKt"
    assert extract_kotlin.facade_name("app/x.kt", "Named") == "Named"
    bom = "﻿".encode()
    kt = _kotlin("A.kt", bom + b"package a\n\nclass A\n")
    java = extract_java.extract("A.java", bom + b"package a;\n\nclass A {}\n")
    assert not kt.degraded and not java.degraded and kt.package == java.package == "a"


def test_implicit_receivers_survive_the_stored_form():
    f = _kotlin()
    back = decode_facts(encode_facts(f))
    assert [c.implicit for c in back.calls] == [c.implicit for c in f.calls]
    assert back.package == f.package == "com.ex.app"


def test_a_schema_5_blob_still_decodes():
    """A graph built before `package` existed is still read by the read-only
    answers until it is rebuilt."""
    import json
    import zlib
    old = zlib.compress(json.dumps(["a.py", "python", False, [], [], [["a.py::f", 1, "g", None, None]], [], [], []]
                                   ).encode())
    f = decode_facts(old)
    assert f.package is None and f.calls[0].implicit is None
