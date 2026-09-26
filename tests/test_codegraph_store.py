"""The graph store survives the failures the council reproduced on Windows.

Each test plants one: a build that dies half way, a reader holding an old
generation open while it is deleted (WinError 32), a build lock left by a
dead process, and a live one that must not be taken.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from singularity.codegraph import build, scope, store
from singularity.config import Config


def _unit(tmp_path: Path, files: dict[str, str]) -> tuple[Config, scope.Unit]:
    root = tmp_path / "Project" / "repo"
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    cfg = Config(vault=tmp_path / "vault", data={"code_repos_dir": str(tmp_path / "Project")})
    (unit,) = scope.discover_units(cfg)
    return cfg, unit


SRC = {"pkg/__init__.py": "", "pkg/a.py": "from .b import g\n\ndef f():\n    return g()\n",
       "pkg/b.py": "def g():\n    return 1\n"}


def test_a_build_writes_a_readable_generation_with_its_versions(tmp_path):
    cfg, unit = _unit(tmp_path, SRC)
    summary = build.build_unit(cfg, unit)
    con = store.connect_ro(store.current_db(cfg, unit.key))
    meta = store.read_meta(con)
    assert meta["schema_version"] == store.SCHEMA_VERSION
    assert meta["versions"]["tree-sitter-python"] == build.parser_versions()["tree-sitter-python"]
    assert build.version_mismatch(meta) == {}
    row = con.execute("SELECT dst, tier FROM edges WHERE src = 'pkg/a.py::f' AND kind = 'CALLS'").fetchone()
    assert tuple(row) == ("pkg/b.py::g", "import")
    assert summary["files"] == 3 and summary["gc_pending"] == []
    con.close()


def test_a_build_that_dies_half_way_leaves_the_previous_generation_current(tmp_path, monkeypatch):
    cfg, unit = _unit(tmp_path, SRC)
    build.build_unit(cfg, unit)
    before = store.current_db(cfg, unit.key)
    (unit.root / "pkg" / "b.py").write_text("def g():\n    return 2\n\ndef h():\n    pass\n", encoding="utf-8")

    def dying_connect(*args, **kwargs):
        con = real_connect(*args, **kwargs)

        class Dying:
            def __getattr__(self, name):
                return getattr(con, name)

            def __enter__(self):
                return con.__enter__()

            def __exit__(self, *exc):
                return con.__exit__(*exc)

            def executemany(self, sql, rows):
                if sql.startswith("INSERT INTO edges"):
                    raise KeyboardInterrupt("killed mid-write")
                return con.executemany(sql, rows)
        return Dying()

    real_connect = store.sqlite3.connect
    monkeypatch.setattr(store.sqlite3, "connect", dying_connect)
    with pytest.raises(KeyboardInterrupt):
        build.build_unit(cfg, unit)
    monkeypatch.undo()
    assert store.current_db(cfg, unit.key) == before
    con = store.connect_ro(before)
    assert con.execute("SELECT count(*) FROM nodes WHERE name = 'h'").fetchone()[0] == 0
    con.close()


@pytest.mark.skipif(os.name != "nt", reason="deleting an open file only fails on Windows")
def test_a_generation_held_open_is_reported_and_removed_on_the_next_build(tmp_path):
    cfg, unit = _unit(tmp_path, SRC)
    build.build_unit(cfg, unit)
    reader = store.connect_ro(store.current_db(cfg, unit.key))
    reader.execute("SELECT count(*) FROM nodes").fetchone()
    held = store.current_db(cfg, unit.key).parent.name
    second = build.build_unit(cfg, unit)
    assert second["gc_pending"] == [held]                 # not silently leaked
    reader.close()
    third = build.build_unit(cfg, unit)
    assert third["gc_pending"] == []
    assert not (store.unit_dir(cfg, unit.key) / held).exists()


def test_a_lock_left_by_a_dead_process_is_taken_over_at_once(tmp_path):
    cfg, unit = _unit(tmp_path, SRC)
    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                          capture_output=True, text=True).stdout.strip()
    lock = store.unit_dir(cfg, unit.key) / ".build.lock"
    lock.mkdir(parents=True)
    (lock / "owner.json").write_text(json.dumps({"pid": int(dead), "create_time": 1.0}), encoding="utf-8")
    assert build.build_unit(cfg, unit)["files"] == 3
    assert not lock.exists()


def test_a_live_owners_lock_is_never_taken(tmp_path, monkeypatch):
    cfg, unit = _unit(tmp_path, SRC)
    monkeypatch.setattr(store, "_LOCK_WAIT_SECONDS", 0.3)
    lock = store.unit_dir(cfg, unit.key) / ".build.lock"
    lock.mkdir(parents=True)
    me = {"pid": os.getpid(), "create_time": store.proc.pid_create_time(os.getpid())}
    (lock / "owner.json").write_text(json.dumps(me), encoding="utf-8")
    with pytest.raises(store.GraphLocked):
        build.build_unit(cfg, unit)
    assert (lock / "owner.json").exists()                 # the loser removed nothing


def test_a_version_mismatch_is_named_package_by_package():
    meta = {"versions": {**build.parser_versions(), "tree-sitter-python": "0.0.1"}}
    assert set(build.version_mismatch(meta)) == {"tree-sitter-python"}
