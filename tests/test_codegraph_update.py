"""Refresh-before-query (CODE-GRAPH P3): the graph follows the tree in place,
equals a full build outside the stale set it declares, writes nothing when
nothing changed, and never answers stale without saying so.

The broad proof is the equivalence oracle (tools/code_graph_oracle.py, 200
random edit sequences, its result in docs/); a slice of it runs here, and
each of the plan's named cases has its own test.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from singularity.codegraph import build, scope, store, update, versions  # noqa: E402
from singularity.config import Config  # noqa: E402
from tools.code_graph_oracle import dump, run_sequence  # noqa: E402

SRC = {
    "pkg/__init__.py": "",
    "pkg/core.py": "def helper():\n    return 1\n\n\ndef run():\n    return helper()\n",
    "pkg/use.py": "from pkg.core import helper\n\n\ndef use():\n    return helper()\n",
    "pkg/guess.py": "def guess(x):\n    return x.only_here()\n",
    "pkg/lone.py": "def only_here():\n    return 2\n",
    "web/package.json": json.dumps({"name": "@t/web"}),
    "web/a.ts": "import { b } from './b';\nexport function a(): void { b(); }\n",
    "web/b.ts": "export function b(): void {}\n",
}


@pytest.fixture(autouse=True)
def _no_background(monkeypatch):
    monkeypatch.setenv("MB_CODE_NO_BACKGROUND", "1")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


def _commit(root: Path, msg: str = "c") -> None:
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", msg)


def _age(root: Path) -> None:
    """Files older than the racy window: their stat can be trusted, so an
    unchanged tree costs no write at all."""
    past = time.time() - 60
    for p in root.rglob("*"):
        if p.is_file() and ".git" not in p.parts:
            os.utime(p, (past, past))


def _unit(tmp_path: Path, files: dict[str, str] = SRC, git: bool = True, vault: str = "vault"):
    root = tmp_path / "Project" / "repo"
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    if git:
        _git(root, "init", "-q")
        _commit(root)
    _age(root)
    cfg = Config(vault=tmp_path / vault, data={"code_repos_dir": str(tmp_path / "Project"),
                                               "code_graph_enabled": True})
    units = scope.discover_units(cfg)
    for u in units:
        build.build_unit(cfg, u, workers=1)
    return cfg, units[0], root


def _full(tmp_path: Path, cfg: Config, key: str) -> dict:
    other = Config(vault=tmp_path / "vault-full", data=dict(cfg.data))
    unit = next(u for u in scope.discover_units(other) if u.key == key)
    build.build_unit(other, unit, workers=1)
    return dump(other, key)


def _db_files(cfg: Config, key: str) -> dict[str, tuple[int, int]]:
    d = store.current_db(cfg, key).parent
    return {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in d.iterdir() if not p.name.endswith("-shm")}


def _written(cfg: Config, key: str) -> tuple:
    """What a write would move: graph.db's size and mtime, and bytes in the
    WAL. An empty WAL a read-only reader creates after a writer removed it
    holds no data (see the unchanged-tree test for the steady state)."""
    files = _db_files(cfg, key)
    return files["graph.db"], files.get("graph.db-wal", (0, 0))[0]


@pytest.mark.parametrize("seed", range(1, 9))
def test_an_updated_graph_equals_a_full_build(seed, tmp_path):
    """A slice of the oracle: random Python and TypeScript edits, half in git."""
    result = run_sequence(seed, 4, git=seed % 2 == 0, base=tmp_path)
    assert result["equal"], json.dumps(result, indent=1)[:3000]


def test_an_unchanged_tree_is_not_written_and_no_watcher_starts(tmp_path):
    cfg, unit, root = _unit(tmp_path)
    (root / "pkg" / "wip.py").write_text("def wip():\n    return 0\n", encoding="utf-8")   # dirty, untracked
    _age(root)
    update.refresh(cfg, unit.key)                    # takes the untracked file in
    # The writer, closing last, removed the WAL; the next read-only reader
    # creates it again, empty (SQLite needs it to read a WAL database). No
    # data is written by that; from there on nothing changes at all.
    update.refresh(cfg, unit.key)
    before, threads = _db_files(cfg, unit.key), threading.active_count()
    assert before.get("graph.db-wal", (0,))[0] == 0
    for _ in range(3):
        fr = update.refresh(cfg, unit.key)
        assert fr.checked and not fr.updated and not fr.stale_files
    assert _db_files(cfg, unit.key) == before         # graph.db and its WAL: same size, same mtime
    assert threading.active_count() == threads
    assert "watchdog" not in sys.modules


def test_a_checkout_that_moves_head_is_followed(tmp_path):
    cfg, unit, root = _unit(tmp_path)
    (root / "pkg" / "core.py").write_text("def helper():\n    return 1\n\n\ndef run2():\n    return helper()\n",
                                          encoding="utf-8")
    _commit(root, "second")
    first = _git(root, "rev-list", "--max-parents=0", "HEAD").strip()
    update.refresh(cfg, unit.key)
    _git(root, "checkout", "-q", first)
    fr = update.refresh(cfg, unit.key)
    assert fr.updated == ["pkg/core.py"]
    assert dump(cfg, unit.key) == _full(tmp_path, cfg, unit.key)
    con = store.connect_ro(store.current_db(cfg, unit.key))
    assert store.read_meta(con)["indexed_head"] == first
    con.close()


def test_an_edit_inside_a_nested_repository_is_seen_by_its_own_unit(tmp_path):
    files = {**SRC, "inner/mod.py": "def inner_fn():\n    return 1\n"}
    root = tmp_path / "Project" / "repo"
    (root / "inner").mkdir(parents=True)
    (root / "inner" / "mod.py").write_text(files["inner/mod.py"], encoding="utf-8")
    _git(root / "inner", "init", "-q")
    _commit(root / "inner")
    cfg, unit, root = _unit(tmp_path, {k: v for k, v in files.items() if not k.startswith("inner/")})
    inner = next(u for u in scope.discover_units(cfg) if u.key == "repo/inner")
    (root / "inner" / "mod.py").write_text("def inner_fn2():\n    return 1\n", encoding="utf-8")
    assert update.refresh(cfg, unit.key).updated == []            # not the parent's file
    assert update.refresh(cfg, inner.key).updated == ["mod.py"]


def test_a_head_that_is_gone_falls_back_to_comparing_every_file(tmp_path):
    cfg, unit, root = _unit(tmp_path)
    db = store.current_db(cfg, unit.key)
    con = __import__("sqlite3").connect(db)
    con.execute("UPDATE meta SET v = ? WHERE k = 'indexed_head'", (json.dumps("f" * 40),))  # gc'd away
    con.commit()
    con.close()
    (root / "pkg" / "lone.py").write_text("def only_here_now():\n    return 2\n", encoding="utf-8")
    _commit(root)
    fr = update.refresh(cfg, unit.key)
    assert fr.head_unreachable and fr.updated == ["pkg/lone.py"]
    assert dump(cfg, unit.key) == _full(tmp_path, cfg, unit.key)
    assert not update.refresh(cfg, unit.key).head_unreachable        # the new head was recorded


def test_dependents_past_the_cap_are_declared_stale_and_named_in_answers(tmp_path, monkeypatch):
    from singularity.codegraph import query
    cfg, unit, root = _unit(tmp_path)
    monkeypatch.setattr(update, "MAX_SYNC_DEPENDENTS", 0)
    (root / "pkg" / "core.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    fr = update.refresh(cfg, unit.key)
    assert fr.updated == ["pkg/core.py"] and fr.stale_files == ["pkg/use.py"]
    g = query.open_graph(cfg, root)
    data = query.callers(g.con, "pkg/core.py::helper", 50)
    env = query.envelope("callers", g, data, {**fr.as_json(), "all_stale": fr.stale_files})
    g.con.close()
    assert env["freshness"]["stale_files"] == ["pkg/use.py"]
    assert [c for c in env["callers"]["items"] if c["file"] == "pkg/use.py"][0]["stale"] is True
    done = update.update_unit(cfg, unit.key)                         # the background job's work
    assert done["mode"] == "update" and done["stale"] == []
    assert dump(cfg, unit.key) == _full(tmp_path, cfg, unit.key)


def test_more_changed_files_than_the_cap_are_left_to_the_background(tmp_path, monkeypatch):
    cfg, unit, root = _unit(tmp_path)
    monkeypatch.setattr(update, "MAX_SYNC_FILES", 1)
    before = _written(cfg, unit.key)
    for rel in ("pkg/core.py", "pkg/lone.py"):
        (root / rel).write_text((root / rel).read_text(encoding="utf-8") + "\n# edit\n", encoding="utf-8")
    fr = update.refresh(cfg, unit.key)
    assert fr.stale_files == ["pkg/core.py", "pkg/lone.py"] and not fr.updated and fr.checked
    assert _written(cfg, unit.key) == before


def test_a_manifest_change_is_a_rebuild_not_a_patch(tmp_path):
    cfg, unit, root = _unit(tmp_path)
    before = _written(cfg, unit.key)
    (root / "web" / "package.json").write_text(json.dumps({"name": "@t/renamed"}), encoding="utf-8")
    fr = update.refresh(cfg, unit.key)
    assert fr.stale_all and "web/package.json" in fr.stale_all
    assert _written(cfg, unit.key) == before
    assert update.update_unit(cfg, unit.key)["mode"] == "rebuild"
    assert update.refresh(cfg, unit.key).stale_all is None


def test_a_writer_killed_mid_update_leaves_the_graph_as_it_was(tmp_path):
    cfg, unit, root = _unit(tmp_path)
    before = dump(cfg, unit.key)
    (root / "pkg" / "core.py").write_text("def helper2():\n    return 1\n", encoding="utf-8")
    script = (
        "import os, sys\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from singularity.codegraph import rows, update\n"
        "from singularity.config import Config\n"
        "def die(*a, **k):\n    os._exit(9)\n"
        "rows.dep_rows = die\n"                   # after files, nodes and facts are written
        f"cfg = Config(vault=__import__('pathlib').Path({str(tmp_path / 'vault')!r}), data={cfg.data!r})\n"
        f"update.refresh(cfg, {unit.key!r})\n")
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert done.returncode == 9, done.stderr
    assert dump(cfg, unit.key) == before
    assert update.refresh(cfg, unit.key).updated == ["pkg/core.py"]
    assert dump(cfg, unit.key) == _full(tmp_path, cfg, unit.key)


def test_concurrent_queries_update_once_and_none_answers_stale(tmp_path):
    """B5 in small: 8 processes refresh at once after 20 edits. One writes,
    the others poll, re-check and find the work done; every one of them then
    reads the updated graph."""
    cfg, unit, root = _unit(tmp_path)
    for i in range(20):
        (root / "pkg" / f"n{i}.py").write_text(f"def f{i}():\n    return helper()\n", encoding="utf-8")
    script = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from singularity.codegraph import store, update\n"
        "from singularity.config import Config\n"
        f"cfg = Config(vault=__import__('pathlib').Path({str(tmp_path / 'vault')!r}), data={cfg.data!r})\n"
        f"fr = update.refresh(cfg, {unit.key!r})\n"
        f"con = store.connect_ro(store.current_db(cfg, {unit.key!r}))\n"
        "n = con.execute(\"SELECT count(*) FROM files WHERE path LIKE 'pkg/n%'\").fetchone()[0]\n"
        "print(json.dumps({'updated': len(fr.updated), 'stale': fr.stale_files, 'seen': n}))\n")
    env = {**os.environ, "MB_CODE_NO_BACKGROUND": "1"}
    procs = [subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, env=env) for _ in range(8)]
    results = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err
        results.append(json.loads(out))
    assert all(r["seen"] == 20 and r["stale"] == [] for r in results)     # no stale answer
    assert sorted(r["updated"] for r in results)[-1] == 20                 # someone applied all 20
    assert dump(cfg, unit.key) == _full(tmp_path, cfg, unit.key)


def test_a_graph_of_an_older_schema_asks_for_a_rebuild(tmp_path):
    cfg, unit, _ = _unit(tmp_path)
    con = __import__("sqlite3").connect(store.current_db(cfg, unit.key))
    con.execute("UPDATE meta SET v = '2' WHERE k = 'schema_version'")
    con.commit()
    con.close()
    fr = update.refresh(cfg, unit.key)
    assert not fr.checked and "rebuild" in fr.stale_all


def test_an_interpreter_without_the_parser_names_the_changes_instead_of_writing(tmp_path, monkeypatch):
    cfg, unit, root = _unit(tmp_path)
    before = _written(cfg, unit.key)
    monkeypatch.setattr(versions, "parser_available", lambda: False)
    (root / "pkg" / "core.py").write_text("def helper():\n    return 3\n", encoding="utf-8")
    fr = update.refresh(cfg, unit.key)
    assert fr.stale_files == ["pkg/core.py"] and "parser is not installed" in fr.note
    assert _written(cfg, unit.key) == before


def test_a_query_does_not_wait_out_a_long_writer(tmp_path, monkeypatch):
    """A background update of thousands of files holds the write lock for a
    long time; a query waits QUERY_WAIT_SECONDS at most, then answers from the
    graph as it stands and names what it could not apply."""
    import sqlite3
    cfg, unit, root = _unit(tmp_path)
    monkeypatch.setattr(update, "QUERY_WAIT_SECONDS", 0.3)
    (root / "pkg" / "lone.py").write_text("def only_here():\n    return 5\n", encoding="utf-8")
    holder = sqlite3.connect(store.current_db(cfg, unit.key), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        fr = update.refresh(cfg, unit.key)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert fr.checked and fr.stale_files == ["pkg/lone.py"] and "another update" in fr.note
    assert update.refresh(cfg, unit.key).updated == ["pkg/lone.py"]


def test_the_update_verb_reports_what_it_did(tmp_path, capsys, monkeypatch):
    from singularity import cli
    from singularity.commands.code import cmd_code
    cfg, unit, root = _unit(tmp_path)
    (root / "pkg" / "lone.py").write_text("def only_here2():\n    return 2\n", encoding="utf-8")
    monkeypatch.chdir(root)
    code = cmd_code(cfg, cli._build_parser().parse_args(["code", "update", "--json"]))
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["mode"] == "update" and out["updated"] == ["pkg/lone.py"]


def test_a_unit_outside_git_with_a_gitignore_is_not_rebuilt_on_every_query(tmp_path):
    """Found on a real repository: outside git a .gitignore decides nothing, so
    the build does not record it -- and detection took it for a watched file
    that had just appeared, asking for a rebuild on every query. The same rule
    for a package.json under a pruned directory (vendor/)."""
    files = {**SRC, ".gitignore": "build/\n", "vendor/lib/package.json": json.dumps({"name": "v"})}
    cfg, unit, root = _unit(tmp_path, files, git=False)
    fr = update.refresh(cfg, unit.key)
    assert fr.checked and fr.stale_all is None and not fr.updated
    (root / "vendor" / "lib" / "package.json").write_text(json.dumps({"name": "v2"}), encoding="utf-8")
    assert update.refresh(cfg, unit.key).stale_all is None
