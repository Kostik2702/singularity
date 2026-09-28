"""Java and Kotlin resolution (CODE-GRAPH D3), against the traps an Android
app sets: Kotlin calling Java and Java calling Kotlin in one module, a
companion reached through @JvmStatic and through `.Companion`, an object
through `INSTANCE`, a file facade (`ItemKt`), extension functions, scope
functions (`let` binds `it`, `apply` binds `this`), a class defined once per
build flavor, a platform call whose name our code also defines, and a name
nothing in reach defines -- which must stay unresolved, never guessed.
"""
from __future__ import annotations

from singularity.codegraph import languages
from singularity.codegraph.resolve_jvm import JvmResolver, source_set

M = "app/src/main/java/com/ex"
FILES = {
    f"{M}/data/Repo.java": (
        "package com.ex.data;\n"
        "public class Repo {\n"
        "    public static Repo create() { return new Repo(); }\n"
        "    public Item load(int id) { return null; }\n"
        "    public void save() {}\n"
        "    public String getName() { return \"\"; }\n"
        "    public int length() { return 0; }\n"
        "}\n"),
    f"{M}/data/Item.kt": (
        "package com.ex.data\n"
        "class Item(val id: Int) {\n"
        "    fun pause() {}\n"
        "    companion object {\n"
        "        @JvmStatic fun of(id: Int) = Item(id)\n"
        "    }\n"
        "}\n"
        "object Registry {\n"
        "    fun add(item: Item) {}\n"
        "}\n"
        "fun Item.describe(): String = \"x\"\n"
        "fun topLevel(): Repo = Repo.create()\n"),
    f"{M}/ui/BaseScreen.java": (
        "package com.ex.ui;\n"
        "public class BaseScreen {\n"
        "    protected void onBase() {}\n"
        "    public void onStart() {}\n"
        "}\n"),
    f"{M}/ui/Listener.java": "package com.ex.ui;\npublic interface Listener {\n    void onEvent();\n}\n",
    f"{M}/ui/Screen.java": (
        "package com.ex.ui;\n"
        "import com.ex.data.Repo;\n"
        "import com.ex.data.Item;\n"
        "import com.ex.data.Registry;\n"
        "import com.ex.data.ItemKt;\n"
        "import com.ex.data.Missing;\n"
        "import com.ex.util.PushUtils;\n"
        "public class Screen extends BaseScreen implements Listener {\n"
        "    private Repo repo = Repo.create();\n"
        "    void show(Listener l) {\n"
        "        Item it = repo.load(1);\n"
        "        it.pause();\n"
        "        Item.of(2);\n"
        "        Item.Companion.of(3);\n"
        "        Registry.INSTANCE.add(it);\n"
        "        ItemKt.topLevel();\n"
        "        PushUtils.register();\n"
        "        onBase();\n"
        "        l.onEvent();\n"
        "        String s = String.valueOf(1);\n"
        "        s.length();\n"
        "        new Missing();\n"
        "        repo.hashCode();\n"
        "    }\n"
        "    public void onEvent() {}\n"
        "}\n"),
    f"{M}/ui/Panel.kt": (
        "package com.ex.ui\n"
        "import com.ex.data.Repo\n"
        "import com.ex.data.Item\n"
        "import com.ex.data.describe\n"
        "import android.app.Notification\n"
        "class Panel(private val repo: Repo, private val builder: Notification.Builder) : BaseScreen() {\n"
        "    fun draw(item: Item?) {\n"
        "        repo.save()\n"
        "        item?.let { it.pause() }\n"
        "        item?.describe()\n"
        "        repo.apply { save() }\n"
        "        super.onStart()\n"
        "        Screen().show(null)\n"
        "        orphan()\n"
        "        repo.name.trim()\n"
        "    }\n"
        "    fun setProgress(p: Int) {\n"
        "        builder.run { setProgress(p) }\n"
        "    }\n"
        "}\n"),
    "app/src/main/java/com/ex/other/Tools.kt": "package com.ex.other\nfun orphan() {}\n",
    # one name, two extensions on different receivers, one package, one source set (the audit's e48)
    f"{M}/ext/FragmentExt.kt": "package com.ex.ext\nfun Fragment.ready(): Boolean = true\n",
    f"{M}/ext/ActivityExt.kt": "package com.ex.ext\nfun AppCompatActivity.ready(f: Any): Boolean = true\n",
    f"{M}/ext/TwinA.kt": "package com.ex.ext\nfun twin() {}\n",
    f"{M}/ext/TwinB.kt": "package com.ex.ext\nfun twin(x: Int) {}\n",
    f"{M}/ui/Host.kt": (
        "package com.ex.ui\n"
        "import com.ex.ext.ready\n"
        "import com.ex.ext.twin\n"
        "class Host : androidx.appcompat.app.AppCompatActivity() {\n"
        "    fun go() {\n"
        "        ready(1)\n"
        "        twin()\n"
        "    }\n"
        "}\n"),
    "app/src/gplay/java/com/ex/util/PushUtils.java":
        "package com.ex.util;\npublic class PushUtils {\n    public static void register() {}\n}\n",
    "app/src/generic/java/com/ex/util/PushUtils.java":
        "package com.ex.util;\npublic class PushUtils {\n    public static void register() {}\n}\n",
    "app/src/gplay/java/com/ex/util/GplayOnly.java":
        "package com.ex.util;\nclass GplayOnly {\n    void go() { PushUtils.register(); }\n}\n",
}
GPLAY = "app/src/gplay/java/com/ex/util/PushUtils.java::PushUtils.register"
GENERIC = "app/src/generic/java/com/ex/util/PushUtils.java::PushUtils.register"


def _resolved():
    facts = {p: languages.extract(p, s.encode())[1] for p, s in FILES.items()}
    resolver = JvmResolver(facts)
    edges, unresolved = resolver.resolve()
    calls: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for e in edges:
        if e.kind == "CALLS":
            calls.setdefault((e.src.split("::")[1], e.callee), set()).add((e.dst.split("::")[1], e.tier))
    return calls, edges, unresolved, resolver


def _targets(calls, caller, callee):
    return {dst for dst, _tier in calls.get((caller, callee), set())}


def _at(edges, line: int, callee: str) -> set[str]:
    return {e.dst.split("::")[1] for e in edges if e.kind == "CALLS" and e.line == line and e.callee == callee}


def test_java_calls_kotlin_members_companions_objects_and_facades():
    calls, edges, _, _ = _resolved()
    assert _targets(calls, "Screen.show", "pause") == {"Item.pause"}              # a local of a Kotlin type
    assert _at(edges, 13, "of") == {"Item.Companion.of"}                          # @JvmStatic, through the class
    assert _at(edges, 14, "of") == {"Item.Companion.of"}                          # through `.Companion`
    assert _targets(calls, "Screen.show", "add") == {"Registry.add"}              # object via INSTANCE
    assert _targets(calls, "Screen.show", "topLevel") == {"topLevel"}             # ItemKt facade
    assert _targets(calls, "Screen.show", "load") == {"Repo.load"}                # a field initialised by a factory


def test_kotlin_calls_java_through_properties_scope_functions_and_extensions():
    calls, _, _, _ = _resolved()
    assert _targets(calls, "Panel.draw", "save") == {"Repo.save"}                 # ctor property and `apply`
    assert _targets(calls, "Panel.draw", "pause") == {"Item.pause"}               # `it` of `item?.let`
    assert _targets(calls, "Panel.draw", "describe") == {"describe"}              # an imported extension
    assert _targets(calls, "Panel.draw", "Screen") == {"Screen"}                  # a constructor
    assert _targets(calls, "Panel.draw", "show") == {"Screen.show"}


def test_inheritance_super_and_interfaces():
    calls, edges, _, _ = _resolved()
    assert _targets(calls, "Screen.show", "onBase") == {"BaseScreen.onBase"}      # implicit this, inherited
    assert _targets(calls, "Screen.show", "onEvent") == {"Listener.onEvent"}      # the STATIC type's method
    assert _targets(calls, "Panel.draw", "onStart") == {"BaseScreen.onStart"}
    inherits = {(e.src.split("::")[1], e.dst.split("::")[1]) for e in edges if e.kind == "INHERITS"}
    assert {("Screen", "BaseScreen"), ("Screen", "Listener"), ("Panel", "BaseScreen")} <= inherits


def test_a_flavor_class_draws_an_edge_to_every_variant_main_can_see():
    """D3: from main, each flavor's PushUtils is a candidate; from gplay, only gplay's."""
    _, edges, _, _ = _resolved()
    from_main = {(e.dst, e.tier) for e in edges if e.kind == "CALLS" and e.callee == "register"
                 and e.src.endswith("Screen.show")}
    assert from_main == {(GPLAY, "variant"), (GENERIC, "variant")}
    from_gplay = {(e.dst, e.tier) for e in edges if e.kind == "CALLS" and e.src.endswith("GplayOnly.go")}
    assert from_gplay == {(GPLAY, "class-attr")}


def test_what_the_rules_cannot_place_is_unresolved_and_never_guessed():
    """`orphan` is defined exactly once, in a package Panel neither is in nor
    imports: a name-unique guess would draw it. The import of a class the
    graph does not hold, into a first-party package, is unresolved -- not
    silently external."""
    calls, _, unresolved, _ = _resolved()
    assert ("Panel.draw", "orphan") not in calls
    assert ("Screen.show", "Missing") not in calls
    names = {(u.src.split("::")[1], u.name) for u in unresolved}
    assert ("Panel.draw", "orphan") in names and ("Screen.show", "Missing") in names


def test_platform_calls_whose_names_we_also_define_draw_nothing():
    calls, _, unresolved, _ = _resolved()
    assert ("Screen.show", "length") not in calls                                 # String.length, not Repo.length
    assert ("Screen.show", "valueOf") not in calls
    assert ("Panel.draw", "trim") not in calls                                    # repo.name -> getName(): String
    # a first-party class "lacking" a method every object has inherits it: not unresolved either
    assert ("Screen.show", "hashCode") not in calls
    assert all(u.name not in ("length", "valueOf", "trim", "hashCode") for u in unresolved)


def test_a_bare_call_in_a_receiver_lambda_of_a_library_type_is_not_its_own_caller():
    """`builder.run { setProgress(p) }` inside `fun setProgress`: the call is
    the Builder's, and an edge back to the function itself would be false."""
    calls, _, _, _ = _resolved()
    assert ("Panel.setProgress", "setProgress") not in calls


def test_same_named_extensions_are_told_apart_by_the_implicit_receiver():
    """The out-of-sample audit's one wrong edge: two `ready` extensions in one
    package drew a `variant` edge to both. The Activity's own supertype --
    outside the graph, known by the name written in its bases -- picks one."""
    _, edges, _, _ = _resolved()
    got = {(e.dst, e.tier) for e in edges if e.kind == "CALLS" and e.callee == "ready"}
    assert got == {(f"{M}/ext/ActivityExt.kt::ready", "import")}


def test_several_definitions_in_one_source_set_are_not_variants():
    """`variant` is one definition per build-flavor source set. Two `twin`
    overloads in src/main are an ambiguity the rules cannot settle."""
    calls, _, unresolved, _ = _resolved()
    assert ("Host.go", "twin") not in calls
    assert ("Host.go", "twin") in {(u.src.split("::")[1], u.name) for u in unresolved}


def test_listing_a_package_is_recorded_as_a_read_of_its_pseudo_path():
    """An update must re-resolve Screen when a file enters or leaves a package
    it listed (see update): the listing is a dependency like any file read."""
    _, _, _, resolver = _resolved()
    deps, _names = resolver.file_deps[f"{M}/ui/Screen.java"]
    assert languages.PACKAGE_DEP + "com.ex.data" in deps
    assert languages.PACKAGE_DEP + "com.ex.util" in deps


def test_source_sets_are_read_from_the_android_layout():
    assert source_set("app/src/gplay/java/a/B.java") == "gplay"
    assert source_set("lib/src/main/kotlin/a/B.kt") == "main"
    assert source_set("B.java") is None
