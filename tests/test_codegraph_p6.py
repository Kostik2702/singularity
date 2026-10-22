"""CODE-GRAPH P6b: what hooks may know of the graph (the sidecar), the one
SessionStart line built from it, and the nightly rebuild that checks the
incrementally maintained graph against a full build.

Planted each time: two writers racing for the sidecar with the lock taken
away, an update of a replaced generation recorded after the build that
replaced it, a store deleted behind the sidecar's back, a hook path that
would import SQLite or open the graph, rows of a graph changed behind the
updater's back, and a unit that would run someone else's `singularity`.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from singularity import config as config_mod
from singularity.codegraph import build, compare, scope, sidecar, store, update, versions
from singularity.config import Config
from singularity.hooks import session_start
from singularity.knowledge import nightly

REPO = Path(__file__).resolve().parents[1]

SRC = {
    "pkg/__init__.py": "",
    "pkg/core.py": "def helper():\n    return 1\n\n\ndef run():\n    return helper()\n",
    "pkg/use.py": "from pkg.core import helper\n\n\ndef use():\n    return helper()\n",
}


@pytest.fixture(autouse=True)
def _no_background(monkeypatch):
    monkeypatch.setenv("MB_CODE_NO_BACKGROUND", "1")


def _age(root: Path) -> None:
    """Older than the racy window, so an unchanged tree costs no write."""
    past = time.time() - 60
    for p in root.rglob("*"):
        if p.is_file():
            os.utime(p, (past, past))


def _repo(tmp_path: Path, name: str, files: dict[str, str] = SRC) -> Path:
    root = tmp_path / "Project" / name
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    _age(root)
    return root


def _cfg(tmp_path: Path) -> Config:
    return Config(vault=tmp_path / "vault", data={"code_repos_dir": str(tmp_path / "Project"),
                                                  "code_graph_enabled": True})


def _unit(tmp_path: Path, name: str = "repo"):
    root = _repo(tmp_path, name)
    cfg = _cfg(tmp_path)
    unit = next(u for u in scope.discover_units(cfg) if u.key == name)
    build.build_unit(cfg, unit, workers=1)
    return cfg, unit, root


def _counts(db: Path) -> dict[str, int]:
    con = store.connect_ro(db)
    try:
        return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("files", "nodes", "edges")}
    finally:
        con.close()


# --- the sidecar ----------------------------------------------------------------------

def test_a_build_leaves_its_entry_in_the_sidecar(tmp_path):
    cfg, unit, root = _unit(tmp_path)
    db = store.current_db(cfg, unit.key)
    entry = sidecar.read(cfg.code_graph_dir)[unit.key]
    assert entry["root"] == str(root) and entry["real_root"] == sidecar.norm(root)
    assert entry["generation"] == db.parent.name and entry["dir"] == store.unit_dir(cfg, unit.key).name
    assert {k: entry[k] for k in ("files", "nodes", "edges")} == _counts(db)
    assert entry["files"] == 3 and entry["edges"] > 0
    assert (entry["stale_count"], entry["stale_all"], entry["seq"], entry["updated_at"]) == (0, None, 0, None)
    assert entry["schema"] == store.SCHEMA_VERSION and entry["built_at"] <= time.time()


def test_an_update_that_writes_moves_the_entry_and_an_unchanged_tree_does_not(tmp_path):
    cfg, unit, root = _unit(tmp_path)
    side = sidecar.path(cfg.code_graph_dir)
    before, stamp = side.read_bytes(), side.stat().st_mtime_ns
    fr = update.refresh(cfg, unit.key)
    assert fr.checked and not fr.updated
    assert side.read_bytes() == before and side.stat().st_mtime_ns == stamp      # nothing written, nothing recorded
    (root / "pkg" / "core.py").write_text(SRC["pkg/core.py"] + "\n\ndef more():\n    return run()\n",
                                          encoding="utf-8")
    fr = update.refresh(cfg, unit.key)
    assert fr.updated == ["pkg/core.py"]
    old = json.loads(before)["units"][unit.key]
    entry = sidecar.read(cfg.code_graph_dir)[unit.key]
    assert entry["seq"] == 1 and entry["updated_at"] >= entry["built_at"]
    assert entry["generation"] == old["generation"] and entry["nodes"] == old["nodes"] + 1
    assert {k: entry[k] for k in ("files", "nodes", "edges")} == _counts(store.current_db(cfg, unit.key))


def _fake_unit(graph_dir: Path, i: int) -> Path:
    """A store layout with nothing in it but CURRENT: `_describe` is replaced
    in the race test, so no database is needed."""
    d = graph_dir / f"u{i}"
    (d / f"gen_{1000 + i}_1").mkdir(parents=True)
    (d / "CURRENT").write_text(f"gen_{1000 + i}_1", encoding="utf-8")
    return d / f"gen_{1000 + i}_1" / "graph.db"


def _race(graph_dir: Path, dbs: list[Path]) -> None:
    gate = threading.Barrier(len(dbs))

    def go(db):
        gate.wait()
        sidecar.record(db)
    threads = [threading.Thread(target=go, args=(db,)) for db in dbs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_two_writers_never_drop_each_others_entry(tmp_path, monkeypatch):
    """Eight writers of eight units at once, each slowed between its read and
    its write so that every one of them reads before anyone writes. Without
    the lock that interleaving keeps one entry of eight -- checked first, so
    this test cannot pass by the race never happening."""
    graph_dir = tmp_path / "code_graph"
    dbs = [_fake_unit(graph_dir, i) for i in range(8)]
    monkeypatch.setattr(sidecar, "_describe", lambda db: {
        "key": db.parent.parent.name, "real_root": str(tmp_path / db.parent.parent.name),
        "dir": db.parent.parent.name, "generation": db.parent.name, "seq": 0})
    real_read, real_locked = sidecar.read, sidecar._locked

    def slow_read(d):
        units = real_read(d)
        time.sleep(0.05)
        return units
    monkeypatch.setattr(sidecar, "read", slow_read)

    monkeypatch.setattr(sidecar, "_locked", lambda d: contextlib.nullcontext())
    _race(graph_dir, dbs)
    assert len(real_read(graph_dir)) < 8, "the race did not happen; this test proves nothing"

    sidecar.path(graph_dir).unlink()
    monkeypatch.setattr(sidecar, "_locked", real_locked)
    _race(graph_dir, dbs)
    assert sorted(real_read(graph_dir)) == [f"u{i}" for i in range(8)]


def test_an_update_of_a_replaced_generation_does_not_undo_the_build(tmp_path, monkeypatch):
    """An in-place update of generation G1 can finish after a build wrote G2:
    its entry is older and must not replace G2's."""
    cfg, unit, _root = _unit(tmp_path)
    graph_dir = cfg.code_graph_dir
    newer = sidecar.read(graph_dir)[unit.key]
    older = {**newer, "generation": "gen_1_1", "seq": 99, "nodes": -1}
    monkeypatch.setattr(sidecar, "_describe", lambda db: dict(older))
    assert sidecar.record(store.current_db(cfg, unit.key)) is False
    assert sidecar.read(graph_dir)[unit.key] == newer
    later = {**newer, "seq": newer["seq"] + 1, "nodes": 7}         # the same generation, updated: replaces
    monkeypatch.setattr(sidecar, "_describe", lambda db: dict(later))
    assert sidecar.record(store.current_db(cfg, unit.key)) is True
    assert sidecar.read(graph_dir)[unit.key]["nodes"] == 7


def test_a_deleted_store_is_no_longer_announced(tmp_path):
    _repo(tmp_path, "one")
    _repo(tmp_path, "two")
    cfg = _cfg(tmp_path)
    units = {u.key: u for u in scope.discover_units(cfg)}
    for u in units.values():
        build.build_unit(cfg, u, workers=1)
    assert sorted(sidecar.read(cfg.code_graph_dir)) == ["one", "two"]
    shutil.rmtree(store.unit_dir(cfg, "one"))
    build.build_unit(cfg, units["two"], workers=1)
    assert sorted(sidecar.read(cfg.code_graph_dir)) == ["two"]


def test_a_failed_recording_never_fails_the_writer(tmp_path, monkeypatch, caplog):
    cfg, unit, _root = _unit(tmp_path)
    monkeypatch.setattr(sidecar, "_describe", lambda db: (_ for _ in ()).throw(sqlite3.OperationalError("x")))
    assert sidecar.record(store.current_db(cfg, unit.key)) is False
    assert "not recorded for hooks" in caplog.text
    build.build_unit(cfg, unit, workers=1)                       # and a build still completes


def test_the_deepest_root_wins_and_a_sibling_prefix_does_not_match(tmp_path):
    outer, inner, sibling = tmp_path / "a", tmp_path / "a" / "b", tmp_path / "a-b"
    for d in (outer / "x", inner / "c", sibling):
        d.mkdir(parents=True)
    units = {k: {"key": k, "real_root": sidecar.norm(p), "dir": k, "generation": "gen_1_1"}
             for k, p in (("outer", outer), ("inner", inner))}
    graph_dir = tmp_path / "code_graph"
    graph_dir.mkdir()
    sidecar._write(graph_dir, units)
    assert sidecar.entry_for(graph_dir, inner / "c")["key"] == "inner"
    assert sidecar.entry_for(graph_dir, inner)["key"] == "inner"
    assert sidecar.entry_for(graph_dir, outer / "x")["key"] == "outer"
    assert sidecar.entry_for(graph_dir, sibling) is None
    sidecar.path(graph_dir).write_text("{not json", encoding="utf-8")
    assert sidecar.read(graph_dir) == {} and sidecar.entry_for(graph_dir, inner) is None


@pytest.mark.parametrize("host", [None, "Build Box/ü:7", "", "x" * 80])
def test_the_hooks_path_to_the_sidecar_is_the_one_config_names(tmp_path, monkeypatch, host):
    """SessionStart finds the sidecar without `platform.node()` (a subprocess
    on Windows); it must land on exactly the directory every writer uses.
    None: this machine's real name, unpatched."""
    if host is not None:
        import platform
        import socket
        monkeypatch.setattr(platform, "node", lambda: host)
        monkeypatch.setattr(socket, "gethostname", lambda: host)
    assert sidecar.graph_dir(tmp_path) == Config(vault=tmp_path).code_graph_dir


# --- SessionStart ---------------------------------------------------------------------

def _session(monkeypatch, cfg: Config, enabled: bool = True) -> None:
    monkeypatch.setattr(config_mod, "resolve_vault", lambda explicit=None: cfg.vault)
    config_mod.CONFIG_FILE.write_text(json.dumps({"code_graph_enabled": enabled}), encoding="utf-8")


def test_session_start_names_the_unit_the_session_opens_in(tmp_path, monkeypatch):
    cfg, unit, root = _unit(tmp_path)
    _session(monkeypatch, cfg)
    ctx = session_start.build_context(str(root / "pkg"))
    (line,) = [ln for ln in ctx.splitlines() if ln.startswith("Code graph:")]
    assert line.startswith(f"Code graph: {unit.key} — 3 files, built 0h ago — ")
    assert "mb code callers" in line and "mb-code" in line
    assert "Code graph:" not in session_start.build_context(str(tmp_path))          # outside every unit
    _session(monkeypatch, cfg, enabled=False)
    assert "Code graph:" not in session_start.build_context(str(root))              # switched off


def test_the_line_says_stale_rather_than_vouching_for_an_old_graph():
    now = time.time()
    entry = {"key": "svc", "files": 12, "built_at": now - 50 * 3600, "updated_at": None, "stale_count": 0}
    line = session_start._code_graph_line(entry, now)
    assert "built 50h ago (stale: nightly has not rebuilt it)" in line
    entry.update(updated_at=now - 3600, stale_count=2)
    line = session_start._code_graph_line(entry, now)
    assert "updated 1h ago," in line and "stale:" not in line and "2 stale file(s)" in line
    entry["stale_all"] = "300 files changed since the graph was built"
    assert "may be stale throughout: 300 files changed" in session_start._code_graph_line(entry, now)


def test_a_broken_sidecar_does_not_break_the_session(tmp_path, monkeypatch):
    cfg, _unit_, root = _unit(tmp_path)
    sidecar.path(cfg.code_graph_dir).write_text("{not json", encoding="utf-8")
    _session(monkeypatch, cfg)
    ctx = session_start.build_context(str(root))
    assert "SEM-FIRST" in ctx and "Code graph:" not in ctx


def test_the_session_start_path_imports_no_parser_and_opens_no_graph(tmp_path):
    """Decision D1: hooks read the sidecar and nothing else. Run in a fresh
    interpreter against a real built graph: no parser, no networkx -- and no
    sqlite3 at all, so no graph.db can have been opened -- and the
    generation's directory is exactly as it was: a read-only SQLite open of a
    WAL database would have created graph.db-shm / graph.db-wal."""
    cfg, unit, root = _unit(tmp_path)
    gen = store.current_db(cfg, unit.key).parent
    for leftover in ("graph.db-shm", "graph.db-wal"):         # left by the build's own reader
        (gen / leftover).unlink(missing_ok=True)
    listing = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in gen.iterdir()}
    home = tmp_path / "home"
    home.mkdir()
    (home / ".singularity.json").write_text(json.dumps({"vault_path": str(cfg.vault), "code_graph_enabled": True}),
                                            encoding="utf-8")
    env = {**os.environ, "USERPROFILE": str(home), "HOME": str(home), "MB_HOME": str(cfg.vault)}
    probe = ("import json, sys\n"
             "from singularity.hooks import session_start\n"
             "ctx = session_start.build_context(sys.argv[1])\n"
             "heavy = ('tree_sitter', 'networkx', 'sqlite3', '_sqlite3', 'numpy', 'sentence_transformers')\n"
             "print(json.dumps({'mods': sorted(m for m in heavy if m in sys.modules), 'ctx': ctx}))\n")
    out = subprocess.run([sys.executable, "-c", probe, str(root)], cwd=str(REPO), env=env,
                         capture_output=True, text=True, encoding="utf-8", check=True).stdout
    data = json.loads(out)
    assert f"Code graph: {unit.key} — 3 files" in data["ctx"]      # the path under test really ran
    assert data["mods"] == []
    assert {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in gen.iterdir()} == listing


# --- the comparator -------------------------------------------------------------------

def _pair(tmp_path: Path):
    cfg, unit, _root = _unit(tmp_path)
    a = store.current_db(cfg, unit.key)
    b = tmp_path / "copy.db"
    compare._snapshot(a, b)
    return a, b


def _edit(db: Path, *sql: str) -> None:
    con = sqlite3.connect(db)
    with con:
        for s in sql:
            con.execute(s)
    con.close()


def test_a_snapshot_is_a_rollback_mode_file_that_opens_read_only(tmp_path):
    """A backup carries the WAL flag over; a WAL file with no `-wal` and
    `-shm` beside it cannot be opened `mode=ro` by every SQLite build, so the
    copy must not be one (bytes 18 and 19 of the header are 1 in rollback
    mode, 2 in WAL). The header, not a failed open, is asserted: a build that
    can open such a file would pass the open and still hide the defect."""
    a, b = _pair(tmp_path)
    assert b.read_bytes()[18:20] == b"\x01\x01"
    assert compare.dump(b)["files"]


def test_the_comparator_sees_a_changed_row_a_missing_row_and_a_duplicate(tmp_path):
    a, b = _pair(tmp_path)
    assert compare.diff_graphs(a, b) == {}
    _edit(b, "DELETE FROM edges WHERE rowid = (SELECT MIN(rowid) FROM edges WHERE kind = 'CALLS')",
          "INSERT INTO edges SELECT * FROM edges WHERE rowid = (SELECT MAX(rowid) FROM edges)",
          "UPDATE nodes SET signature = 'def run(changed)' WHERE name = 'run'")
    got = compare.diff_graphs(a, b)
    assert sorted(got) == ["edges", "nodes"]
    assert got["edges"]["differing"] == [1, 1] and got["edges"]["counts"][0] == got["edges"]["counts"][1]
    assert len(got["edges"]["update_only"]) == 1 and len(got["edges"]["build_only"]) == 1
    assert got["nodes"]["build_only"][0][6] == "def run(changed)"
    # the in-memory form the tests use agrees on what differs (a duplicate is
    # visible to it only in the counts, the multiset form names the row)
    whole = compare.diff(compare.dump(a), compare.dump(b))
    assert sorted(whole) == sorted(got) and all(whole[t]["counts"] == got[t]["counts"] for t in got)


def test_rows_of_a_file_declared_stale_are_left_out_on_both_sides(tmp_path):
    a, b = _pair(tmp_path)
    _edit(b, "DELETE FROM edges WHERE file_id = (SELECT id FROM files WHERE path = 'pkg/use.py')")
    assert "edges" in compare.diff_graphs(a, b)
    _edit(a, "INSERT INTO stale(path, reason) VALUES ('pkg/use.py', 'test')")
    assert compare.diff_graphs(a, b) == {}


# --- nightly --------------------------------------------------------------------------

def _machine_config(tmp_path: Path, monkeypatch, cfg: Config) -> None:
    """The `mb code` children read the machine config from their home, not
    this test's Config object: give them a home of their own."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".singularity.json").write_text(json.dumps({"vault_path": str(cfg.vault), **cfg.data}),
                                            encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))


def test_nightly_skips_while_the_graph_is_off_or_cannot_be_built(tmp_path, monkeypatch):
    now = "2026-09-27T03:00:00"
    assert nightly._step_code_graph(Config(vault=tmp_path, data={}), now) == {
        "status": "skipped", "reason": "code_graph_enabled=false"}
    cfg = _cfg(tmp_path)
    assert nightly._step_code_graph(cfg, now)["reason"] == "no code graph built on this machine"
    monkeypatch.setattr(versions, "parser_available", lambda: False)
    got = nightly._step_code_graph(cfg, now)
    assert got["status"] == "skipped" and sys.executable in got["reason"]


def test_nightly_rebuilds_each_unit_and_finds_no_difference(tmp_path, monkeypatch):
    cfg, unit, _root = _unit(tmp_path)
    _machine_config(tmp_path, monkeypatch, cfg)
    before = store.current_db(cfg, unit.key).parent.name
    log = cfg.code_graph_dir / nightly.CODE_GRAPH_LOG_NAME
    log.write_text(json.dumps({"ts": "2020-01-01T00:00:00", "units": {}}) + "\n", encoding="utf-8")
    got = nightly._step_code_graph(cfg, "2026-09-27T03:00:00")
    assert got["status"] == "ok", json.dumps(got, indent=1)
    result = got["units"][unit.key]
    assert result["status"] == "equal" and result["incremental"] == "update"
    assert result["generation_before"] == before != result["generation"]
    assert store.current_db(cfg, unit.key).parent.name == result["generation"]
    assert sidecar.read(cfg.code_graph_dir)[unit.key]["generation"] == result["generation"]   # the child recorded it
    assert not (store.unit_dir(cfg, unit.key) / compare.SNAPSHOT_NAME).exists()
    rows = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()]
    assert [r["ts"] for r in rows] == ["2026-09-27T03:00:00"]            # the old row aged out


def test_nightly_catches_the_graph_up_before_comparing(tmp_path, monkeypatch):
    """An edit nobody queried since is not a divergence: the maintained graph
    is updated first, and only then held against the rebuild."""
    cfg, unit, root = _unit(tmp_path)
    _machine_config(tmp_path, monkeypatch, cfg)
    (root / "pkg" / "use.py").write_text(SRC["pkg/use.py"] + "\n\ndef again():\n    return use()\n",
                                         encoding="utf-8")
    got = nightly._step_code_graph(cfg, "2026-09-27T03:00:00")
    assert got["status"] == "ok", json.dumps(got, indent=1)
    assert got["units"][unit.key]["status"] == "equal"


def test_nightly_warns_when_the_updated_graph_differs_from_a_rebuild(tmp_path, monkeypatch):
    """The plant: a CALLS edge disappears from the maintained graph while the
    tree stays as it was -- what an incremental bug looks like from outside.
    The rebuild has it back; the step says which table, how many rows, and
    shows the row."""
    cfg, unit, _root = _unit(tmp_path)
    _machine_config(tmp_path, monkeypatch, cfg)
    db = store.current_db(cfg, unit.key)
    con = sqlite3.connect(db)
    lost = con.execute("SELECT src, dst FROM edges WHERE kind = 'CALLS' ORDER BY src LIMIT 1").fetchone()
    with con:
        con.execute("DELETE FROM edges WHERE kind = 'CALLS' AND src = ? AND dst = ?", lost)
    con.close()
    result = {"steps": {"code_graph": nightly._step_code_graph(cfg, "2026-09-27T03:00:00")}}
    got = result["steps"]["code_graph"]
    assert got["status"] == "warn" and not nightly.has_error(result)
    unit_result = got["units"][unit.key]
    assert unit_result["status"] == "differs" and "tree_changed_during_check" not in unit_result
    assert unit_result["diff"]["edges"]["differing"] == [0, 1]
    assert tuple(unit_result["diff"]["edges"]["build_only"][0][1:3]) == lost        # (src, dst)
    assert f"{unit.key} (edges 0/1)" in got["reason"] and got["log"] in got["reason"]
    logged = json.loads((cfg.code_graph_dir / nightly.CODE_GRAPH_LOG_NAME).read_text(encoding="utf-8"))
    assert logged["units"][unit.key]["diff"]["edges"]["differing"] == [0, 1]


def test_a_failed_rebuild_is_an_error_that_names_the_unit(tmp_path, monkeypatch):
    cfg, unit, _root = _unit(tmp_path)
    monkeypatch.setattr(compare, "_mb_code", lambda cfg, root, verb, timeout: {
        "error": f"`mb code {verb}` exited with 2", "stderr": "boom"})
    result = {"steps": {"code_graph": nightly._step_code_graph(cfg, "2026-09-27T03:00:00")}}
    got = result["steps"]["code_graph"]
    assert got["status"] == "error" and nightly.has_error(result)
    assert got["error"] == f"{unit.key}: `mb code update` exited with 2"
    assert got["counts"] == {"equal": 0, "differs": 0, "skipped": 0, "error": 1}
    assert got["units"][unit.key]["stderr"] == "boom"


def test_a_unit_holding_another_singularity_is_not_run(tmp_path):
    cfg, unit, root = _unit(tmp_path)
    (root / "singularity").mkdir()
    (root / "singularity" / "__init__.py").write_text("", encoding="utf-8")
    got = compare.rebuild_and_compare(cfg, {"key": unit.key, "root": str(root)})
    assert got["status"] == "skipped" and "its own `singularity` package" in got["reason"]
