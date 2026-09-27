"""impact, deadcode, arch (CODE-GRAPH P4), each against the plan's acceptance:
a planted TypeScript test that calls a changed function shows up in impact;
a planted function nothing calls is found dead; hook entry points, common
method names and dunders are not; the architecture is the same partition,
under the same numbers, every time it is built.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from singularity.codegraph import analysis, build, query, scope, store  # noqa: E402
from singularity.codegraph.impact import impact  # noqa: E402
from singularity.config import Config  # noqa: E402

SRC = {
    "pyproject.toml": '[project]\nname = "app"\n\n[project.scripts]\napp = "app.cli:main"\n',
    "app/__init__.py": "",
    "app/cli.py": "from app.core import compute\n\n\ndef main():\n    return compute(1)\n",
    "app/core.py": (
        "def compute(x):\n    return helper(x) + 1\n\n\n"
        "def helper(x):\n    return x * 2\n\n\n"
        "def never_called(y):\n    return y - 1\n\n\n"
        "def as_callback(v):\n    return v\n\n\n"
        "def uses_callback(items):\n    return list(map(as_callback, items))\n\n\n"
        "class Engine:\n"
        "    def __repr__(self):\n        return 'e'\n\n"
        "    def run(self):\n        return 1\n\n\n"
        "def drive(thing):\n    return thing.run()\n"),
    "app/hook.py": "def main():\n    return 0\n\n\nif __name__ == '__main__':\n    main()\n",
    "app/reg.py": "import functools\n\n\n@functools.lru_cache\ndef cached():\n    return 1\n",
    "web/package.json": json.dumps({"name": "@t/web"}),
    "web/price.ts": "export function price(n: number): number {\n  return n * 2;\n}\n\n"
                    "function unusedTs(): void {}\n",
    "web/price.test.ts": "import { price } from './price';\n\nit('prices', () => {\n  expect(price(2)).toBe(4);\n});\n",
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


def _commit(root: Path, msg: str = "c") -> None:
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", msg)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("MB_CODE_NO_BACKGROUND", "1")
    root = tmp_path / "Project" / "repo"
    for rel, text in SRC.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    _git(root, "init", "-q")
    _commit(root)
    past = time.time() - 60
    for p in root.rglob("*"):
        if p.is_file() and ".git" not in p.parts:
            os.utime(p, (past, past))
    cfg = Config(vault=tmp_path / "vault", data={"code_repos_dir": str(tmp_path / "Project"),
                                                 "code_graph_enabled": True})
    (unit,) = scope.discover_units(cfg)
    build.build_unit(cfg, unit, workers=1)
    return cfg, unit, root


def _graph(cfg, root):
    return query.open_graph(cfg, root)


def _update(cfg, unit):
    from singularity.codegraph import update
    update.refresh(cfg, unit.key)


# --- impact -------------------------------------------------------------------------

def test_a_test_that_calls_a_changed_function_is_in_the_impact(repo):
    cfg, unit, root = repo
    (root / "web" / "price.ts").write_text(SRC["web/price.ts"].replace("n * 2", "n * 3"), encoding="utf-8")
    _update(cfg, unit)
    g = _graph(cfg, root)
    data = impact(g.con, root, None)
    g.con.close()
    assert {"symbol": "web/price.ts::price", "change": "modified", "file": "web/price.ts"} in data["changed"]["symbols"]
    assert data["tests"] == ["web/price.test.ts"]
    assert any(i["symbol"] == "web/price.test.ts::<module>" and i["test"] for i in data["impacted"]["items"])


def test_callers_are_walked_back_to_the_entry_point(repo):
    cfg, unit, root = repo
    core = SRC["app/core.py"].replace("return x * 2", "return x * 4")
    (root / "app" / "core.py").write_text(core, encoding="utf-8")
    _update(cfg, unit)
    g = _graph(cfg, root)
    data = impact(g.con, root, None, depth=3)
    g.con.close()
    reached = {i["symbol"]: i["depth"] for i in data["impacted"]["items"]}
    assert [s["symbol"] for s in data["changed"]["symbols"]] == ["app/core.py::helper"]
    assert reached == {"app/core.py::compute": 1, "app/cli.py::main": 2}


def test_a_deleted_function_names_its_now_broken_call_sites_and_a_rename_is_a_rename(repo):
    cfg, unit, root = repo
    core = (SRC["app/core.py"].replace("def helper(x):", "def doubled(x):")
            .replace("def never_called(y):\n    return y - 1\n\n\n", ""))
    (root / "app" / "core.py").write_text(core, encoding="utf-8")
    _update(cfg, unit)
    g = _graph(cfg, root)
    data = impact(g.con, root, None)
    g.con.close()
    changes = {s["symbol"]: s for s in data["changed"]["symbols"]}
    assert changes["app/core.py::doubled"]["change"] == "renamed"
    assert changes["app/core.py::doubled"]["was"] == "app/core.py::helper"
    assert changes["app/core.py::never_called"]["change"] == "deleted"
    assert [(b["name"], b["caller"]) for b in data["possibly_broken"]] == [("helper", "app/core.py::compute")]


def test_a_branch_is_compared_with_its_merge_base(repo):
    cfg, unit, root = repo
    _git(root, "checkout", "-q", "-b", "feature")
    (root / "app" / "hook.py").write_text(SRC["app/hook.py"].replace("return 0", "return 2"), encoding="utf-8")
    _commit(root, "feature")
    _update(cfg, unit)
    g = _graph(cfg, root)
    against_head = impact(g.con, root, None)
    against_master = impact(g.con, root, "master" if "master" in _git(root, "branch") else "main")
    g.con.close()
    assert against_head["changed"]["symbols"] == []                     # nothing uncommitted
    assert [s["symbol"] for s in against_master["changed"]["symbols"]] == ["app/hook.py::main"]


# --- deadcode -------------------------------------------------------------------------

def test_an_uncalled_function_is_dead_and_what_is_used_otherwise_is_not(repo):
    cfg, _unit, root = repo
    g = _graph(cfg, root)
    data = analysis.deadcode(g.con, root, g.meta, show_unknown=True)
    g.con.close()
    dead = {d["qname"] for d in data["dead"]["items"]}
    unknown = {d["qname"]: d["why"] for d in data["unknown"]["items"]["items"]}
    assert "app/core.py::never_called" in dead                      # planted: nothing calls it
    assert "web/price.ts::unusedTs" in dead                          # not exported, nothing calls it
    for alive in ("app/cli.py::main",                                # pyproject script
                  "app/hook.py::main",                               # called under __main__
                  "app/core.py::Engine.__repr__",                     # dunder
                  "app/core.py::compute", "web/price.ts::price"):     # called
        assert alive not in dead and alive not in unknown, alive
    assert "resolver could not place" in unknown["app/core.py::Engine.run"]    # `thing.run()`, untyped
    assert "mentioned" in unknown["app/core.py::as_callback"]                  # map(as_callback, ...)
    assert "decorator" in unknown["app/reg.py::cached"]


def test_a_name_used_only_as_a_string_is_not_dead(repo):
    cfg, unit, root = repo
    (root / "app" / "table.py").write_text(
        "def handler_a():\n    return 1\n\n\nHANDLERS = {'a': 'handler_a'}\n", encoding="utf-8")
    _update(cfg, unit)
    g = _graph(cfg, root)
    data = analysis.deadcode(g.con, root, g.meta, show_unknown=True)
    g.con.close()
    assert "app/table.py::handler_a" not in {d["qname"] for d in data["dead"]["items"]}


# --- arch -------------------------------------------------------------------------

def test_the_partition_is_the_same_every_build_and_a_community_keeps_its_number(repo):
    cfg, unit, root = repo

    def snapshot():
        con = store.connect_ro(store.current_db(cfg, unit.key))
        rows = sorted(tuple(r) for r in con.execute(
            "SELECT f.path, c.community FROM communities c JOIN files f ON f.id = c.file_id"))
        con.close()
        return rows
    first = snapshot()
    assert first, "the fixture has cross-file calls, so it has communities"
    build.build_unit(cfg, unit, workers=1)
    assert snapshot() == first                                  # same graph, same seed: same partition
    (root / "app" / "extra.py").write_text("from app.core import compute\n\n\ndef more():\n    return compute(2)\n",
                                           encoding="utf-8")
    build.build_unit(cfg, unit, workers=1)
    after = dict(snapshot())
    assert all(after.get(path) == cid for path, cid in first)   # nobody renumbered


def test_arch_names_its_communities_and_says_when_it_is_behind(repo):
    cfg, unit, root = repo
    g = _graph(cfg, root)
    view = analysis.arch_view(g.con, g.meta)
    g.con.close()
    assert view["communities"]["total"] >= 1 and all(c["label"] for c in view["communities"]["items"])
    assert view["computed"]["updates_since"] == 0
    (root / "app" / "hook.py").write_text(SRC["app/hook.py"].replace("return 0", "return 5"), encoding="utf-8")
    _update(cfg, unit)
    g = _graph(cfg, root)
    view = analysis.arch_view(g.con, g.meta)
    g.con.close()
    assert view["computed"]["updates_since"] == 1 and "in-place update" in view["computed"]["note"]
