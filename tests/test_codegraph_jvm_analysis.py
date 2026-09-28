"""impact and deadcode on an Android module (CODE-GRAPH D3): a Kotlin caller of
a changed Java method is in the impact; what the framework starts -- a
manifest's components, a layout's views and click handlers, a class Dagger
builds, a test the runner runs, a runner the Gradle script names, a Kotlin
override -- is not dead, and a class nothing starts or names is.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from singularity.codegraph import analysis, build, query, scope
from singularity.codegraph.impact import impact
from singularity.config import Config

M = "app/src/main"
J = f"{M}/java/com/ex"
SRC = {
    "app/build.gradle.kts": 'android { defaultConfig { testInstrumentationRunner = "com.ex.Runner" } }\n',
    f"{M}/AndroidManifest.xml": (
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android">\n'
        '  <application android:name=".App">\n'
        '    <activity android:name=".ui.MainActivity" android:exported="true" />\n'
        '    <service android:name="com.ex.sync.SyncService" />\n'
        '  </application>\n</manifest>\n'),
    f"{M}/res/layout/main.xml": (
        '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android">\n'
        '  <com.ex.ui.RoundView android:layout_width="1dp" android:onClick="onRoundClicked" />\n'
        '</LinearLayout>\n'),
    f"{J}/App.kt": "package com.ex\n\nclass App\n",
    f"{J}/Runner.kt": "package com.ex\n\nclass Runner\n",
    f"{J}/ui/MainActivity.kt": (
        "package com.ex.ui\n\nimport com.ex.data.Repo\n\n"
        "class MainActivity : BaseActivity() {\n"
        "    private val repo = Repo()\n\n"
        "    fun show() {\n        repo.load(1)\n    }\n\n"
        "    fun onRoundClicked(v: Any) {\n    }\n\n"
        "    override fun onResume() {\n        show()\n    }\n}\n"),
    f"{J}/ui/BaseActivity.java": (
        "package com.ex.ui;\n\npublic class BaseActivity extends android.app.Activity {\n"
        "    void onResume() {}\n}\n"),
    f"{J}/ui/RoundView.java": "package com.ex.ui;\n\npublic class RoundView {\n}\n",
    f"{J}/sync/SyncService.java": "package com.ex.sync;\n\npublic class SyncService {\n}\n",
    f"{J}/data/Repo.java": (
        "package com.ex.data;\n\npublic class Repo {\n"
        "    public int load(int id) {\n        return id;\n    }\n}\n"),
    f"{J}/data/Store.kt": "package com.ex.data\n\nclass Store @Inject constructor(private val repo: Repo)\n",
    f"{J}/data/Leftover.java": "package com.ex.data;\n\npublic class Leftover {\n    void unused() {}\n}\n",
    # a library builds these by reflection, from a Java resource (the audit's d19)
    f"{M}/resources/ical4j.properties": "net.fortuna.ical4j.timezone.registry=com.ex.data.TzFactory\n",
    f"{J}/data/TzFactory.java": "package com.ex.data;\n\npublic class TzFactory {\n}\n",
    f"{M}/resources/META-INF/services/com.lib.Plugin": "com.ex.data.LoadedPlugin\n",
    f"{J}/data/LoadedPlugin.kt": "package com.ex.data\n\nclass LoadedPlugin\n",
    # a Parcelable's CREATOR: a companion overriding a library interface, called by the framework
    f"{J}/data/Parcel.kt": (
        "package com.ex.data\n\nclass Parcel {\n"
        "    companion object CREATOR : android.os.Parcelable.Creator<Parcel> {\n"
        "        override fun newArray(size: Int): Array<Parcel?> = arrayOfNulls(size)\n    }\n}\n"),
    "app/src/test/java/com/ex/data/RepoTest.kt": (
        "package com.ex.data\n\nclass RepoTest {\n    @Test\n    fun loads() {\n        Repo().load(2)\n    }\n}\n"),
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def android(tmp_path, monkeypatch):
    monkeypatch.setenv("MB_CODE_NO_BACKGROUND", "1")
    root = tmp_path / "Project" / "android"
    for rel, text in SRC.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "c")
    past = time.time() - 60
    for p in root.rglob("*"):
        if p.is_file() and ".git" not in p.parts:
            os.utime(p, (past, past))
    cfg = Config(vault=tmp_path / "vault", data={"code_repos_dir": str(tmp_path / "Project"),
                                                 "code_graph_enabled": True})
    (unit,) = scope.discover_units(cfg)
    build.build_unit(cfg, unit, workers=1)
    return cfg, unit, root


def test_a_kotlin_caller_of_a_changed_java_method_is_in_the_impact(android):
    from singularity.codegraph import update
    cfg, unit, root = android
    repo = root / J / "data" / "Repo.java"
    repo.write_text(SRC[f"{J}/data/Repo.java"].replace("return id;", "return id + 1;"), encoding="utf-8")
    update.refresh(cfg, unit.key)
    g = query.open_graph(cfg, root)
    data = impact(g.con, root, None)
    g.con.close()
    reached = {i["symbol"].split("::")[1] for i in data["impacted"]["items"]}
    assert f"{J}/data/Repo.java::Repo.load" in [s["symbol"] for s in data["changed"]["symbols"]]
    assert {"MainActivity.show", "RepoTest.loads"} <= reached
    assert data["tests"] == ["app/src/test/java/com/ex/data/RepoTest.kt"]


def test_what_android_starts_is_alive_and_what_nothing_names_is_dead(android):
    cfg, _unit, root = android
    g = query.open_graph(cfg, root)
    out = analysis.deadcode(g.con, root, g.meta, limit=100, show_unknown=True)
    g.con.close()
    dead = {d["qname"].split("::")[1] for d in out["dead"]["items"]}
    unknown = {d["qname"].split("::")[1]: d["why"] for d in out["unknown"]["items"]["items"]}
    assert dead == {"Leftover", "Leftover.unused"}
    for alive in ("App", "MainActivity", "SyncService", "RoundView", "Runner", "RepoTest", "TzFactory",
                  "LoadedPlugin"):
        assert alive not in dead and alive not in unknown, alive     # entries: not candidates at all
    assert "layout" in unknown["MainActivity.onRoundClicked"]
    assert "framework may build it" in unknown["Store"]
    assert "override" in unknown["MainActivity.onResume"]
    assert "outside the graph" in unknown["Parcel.CREATOR.newArray"]     # an object's override, too
