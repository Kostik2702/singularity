"""Maintenance of the code graph (CODE-GRAPH P6): doctor K2, the backup pin, `mb stats`.

K2 is WARN only, by decision. Each plant -- a stale set, a graph stale as a
whole, an older schema, other parser/resolver versions, a damaged or an
unreadable database, a share of degraded parses, a generation nobody
collected, a stored secret, a graph never audited, a missing parser, a store
over its size -- must turn it WARN and never FAIL; a healthy graph is PASS,
a switched-off one SKIP.

`mb backup` never copies a graph (decision D1: no graph backup in v1): the
backup of a machine that has one holds no graph file, under any name.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from singularity import doctor, render
from singularity.codegraph import audit, build, scope, store, versions
from singularity.config import Config
from singularity.knowledge import stats
from singularity.machine import backup

FILES = {"pkg/__init__.py": "", **{f"pkg/m{i}.py": f"def f{i}():\n    return {i}\n" for i in range(12)}}
N_FILES = len(FILES)
GITHUB = "gh" + "p_" + "a1B2c3D4e5" * 4          # a github-token shape, assembled at run time


@pytest.fixture(autouse=True)
def _no_background(monkeypatch):
    monkeypatch.setenv("MB_CODE_NO_BACKGROUND", "1")


def _cfg(tmp_path: Path, **data) -> Config:
    return Config(vault=tmp_path / "vault", data={"code_repos_dir": str(tmp_path / "Project"),
                                                  "code_graph_enabled": True, **data})


def _build(cfg: Config, files: dict[str, str] = FILES) -> None:
    root = Path(cfg.data["code_repos_dir"]) / "svc"
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    (unit,) = scope.discover_units(cfg)
    build.build_unit(cfg, unit, workers=1)


@pytest.fixture
def graph(tmp_path) -> Config:
    cfg = _cfg(tmp_path)
    _build(cfg)
    return cfg


def _k2(cfg: Config) -> dict:
    rep = doctor.Report(quiet=True)
    doctor.code_graph_block(rep, cfg)
    (check,) = [c for c in rep.checks if c["id"] == "K2-code-graph"]
    return check


def _sql(cfg: Config, sql: str, *params) -> None:
    with store.write_transaction(store.current_db(cfg, "svc")) as con:
        con.execute(sql, params)


def _set_meta(cfg: Config, key: str, value) -> None:
    _sql(cfg, "INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)", key, json.dumps(value))


# --- doctor K2 -----------------------------------------------------------------------------

def test_a_healthy_audited_graph_passes(graph):
    check = _k2(graph)
    assert check["status"] == "PASS", check
    assert f"1 graph(s), {N_FILES} files" in check["detail"] and "audited clean" in check["detail"]


def test_a_switched_off_graph_is_skipped_and_what_stays_on_disk_is_said(graph):
    graph.data["code_graph_enabled"] = False
    check = _k2(graph)
    assert check["status"] == "SKIP" and "1 graph(s) still on disk" in check["detail"]


def test_on_but_never_built_warns(tmp_path):
    check = _k2(_cfg(tmp_path))
    assert check["status"] == "WARN" and "no graph built" in check["detail"]


def _plant_orphan(cfg: Config, monkeypatch) -> None:
    current = store.current_db(cfg, "svc").parent
    shutil.copytree(current, current.with_name("gen_1_1"))


def _plant_unreadable(cfg: Config, monkeypatch) -> None:
    store.current_db(cfg, "svc").write_bytes(b"this is not a database at all " * 200)


def _plant_secret(cfg: Config, monkeypatch) -> None:
    _build(cfg, {**FILES, "pkg/leak.py": f'def connect(token="{GITHUB}"):\n    return token\n'})


PLANTS = {
    "stale set": (lambda cfg, mp: _sql(cfg, "INSERT INTO stale(path, reason) VALUES ('pkg/m1.py', 'planted')"),
                  "1 stale file(s)"),
    "stale as a whole": (lambda cfg, mp: _set_meta(cfg, "stale_all", "6000 files wait (planted)"),
                         "stale as a whole: 6000 files wait"),
    "older schema": (lambda cfg, mp: _set_meta(cfg, "schema_version", store.SCHEMA_VERSION - 1),
                     f"schema {store.SCHEMA_VERSION - 1}, this engine writes {store.SCHEMA_VERSION}"),
    "other versions": (lambda cfg, mp: _set_meta(cfg, "versions", {**versions.parser_versions(), "resolver": "0"}),
                       "other parser/resolver versions (resolver 0->"),
    "degraded parses": (lambda cfg, mp: _sql(cfg, "UPDATE files SET parse_status = 'partial'"),
                        f"{N_FILES} of {N_FILES} files parsed with errors"),
    "never audited": (lambda cfg, mp: _sql(cfg, "DELETE FROM meta WHERE k = ?", audit.RECORD_KEY),
                      "never audited for secrets"),
    "stored secret": (_plant_secret, "secret-shaped value(s) stored (github-token"),
    "orphaned generation": (_plant_orphan, "1 old generation(s)"),
    "damaged": (lambda cfg, mp: mp.setattr(doctor, "_quick_check", lambda con, budget: "row 3 missing from index"),
                "quick_check: row 3 missing from index"),
    "unreadable": (_plant_unreadable, "could not be read"),
    "parser absent": (lambda cfg, mp: mp.setattr(versions, "parser_available", lambda: False),
                      "the parser is not installed here"),
    "store too large": (lambda cfg, mp: mp.setattr(doctor, "_GRAPH_DISK_WARN_BYTES", 1), "GB, over"),
}


@pytest.mark.parametrize("name", sorted(PLANTS))
def test_each_planted_problem_is_a_warn_never_a_fail(name, graph, monkeypatch):
    plant, said = PLANTS[name]
    plant(graph, monkeypatch)
    check = _k2(graph)
    assert check["status"] == "WARN", check
    assert said in check["detail"], check["detail"]
    assert check["fix"]


def test_a_crash_of_the_check_itself_is_a_warn(graph, monkeypatch):
    monkeypatch.setattr(store, "built_units", lambda cfg: 1 / 0)
    check = _k2(graph)
    assert check["status"] == "WARN" and "check crashed" in check["detail"]


def test_a_few_broken_files_in_a_small_unit_are_not_a_grammar_problem(graph):
    _sql(graph, "UPDATE files SET parse_status = 'partial' WHERE path IN "
                "('pkg/m0.py', 'pkg/m1.py', 'pkg/m2.py', 'pkg/m3.py', 'pkg/m4.py', 'pkg/m5.py', "
                "'pkg/m6.py', 'pkg/m7.py', 'pkg/m8.py')")
    assert _k2(graph)["status"] == "PASS"             # 9 < _GRAPH_DEGRADED_MIN_FILES


def test_updates_since_the_audit_are_said_not_warned(graph):
    _set_meta(graph, "seq", 3)
    check = _k2(graph)
    assert check["status"] == "PASS" and "3 in-place update(s) since the last audit" in check["detail"]


def test_a_generation_being_built_is_not_an_orphan(graph):
    current = store.current_db(graph, "svc").parent
    with store.build_lock(graph, "svc"):
        current.with_name("gen_9999999999999999999_1").mkdir()
        check = _k2(graph)
    assert check["status"] == "PASS" and "a build is running" in check["detail"]


def test_quick_check_is_bounded_by_its_budget(tmp_path):
    con = sqlite3.connect(tmp_path / "big.db")
    con.execute("CREATE TABLE t(a INTEGER, b TEXT)")
    con.executemany("INSERT INTO t VALUES (?, ?)", ((i, str(i) * 8) for i in range(50_000)))
    con.execute("CREATE INDEX t_b ON t(b)")
    con.commit()
    assert doctor._quick_check(con, 10.0) == "ok"
    assert doctor._quick_check(con, -1.0) is None     # interrupted, and said so
    assert con.execute("SELECT count(*) FROM t").fetchone()[0] == 50_000   # the handler is gone again
    con.close()


RUN_PROBE = """
import json, sys
from pathlib import Path
from singularity import config as config_mod
config_mod.CONFIG_FILE = Path(sys.argv[3])          # never the machine's own config (see conftest)
from singularity import doctor
from singularity.platform import scheduler
doctor._run_shim = lambda sh, shim, cwd=None: (0, "mb 0.0.0", "")
scheduler.verify = lambda job: {"registered": False}
cfg = config_mod.Config(vault=Path(sys.argv[1]), data={"code_repos_dir": sys.argv[2], "code_graph_enabled": True})
doctor.run(cfg, as_json=True)
"""


def test_doctor_runs_k2(graph, tmp_path):
    """The whole `mb doctor` run, in its own process: its other checks import
    the watcher (and watchdog), which later tests of this process must not see."""
    out = subprocess.run([sys.executable, "-c", RUN_PROBE, str(graph.vault), graph.data["code_repos_dir"],
                          str(tmp_path / "machine-config.json")],
                         capture_output=True, text=True, encoding="utf-8",
                         env={**os.environ, "PYTHONIOENCODING": "utf-8"}).stdout
    checks = json.loads(out.strip().splitlines()[-1])["checks"]
    assert [c["status"] for c in checks if c["id"] == "K2-code-graph"] == ["PASS"]


# --- the backup pin ------------------------------------------------------------------------

SQLITE_HEADER = b"SQLite format 3" + bytes(1)


def test_backup_never_copies_a_graph_and_restore_never_touches_one(graph):
    graph.save()                                        # a config to back up
    (graph.sources_dir / "doc.md").parent.mkdir(parents=True, exist_ok=True)
    (graph.sources_dir / "doc.md").write_text("# raw source\n", encoding="utf-8")
    db = store.current_db(graph, "svc")
    before = db.read_bytes()

    manifest = backup.create(graph, now="B1", with_sources=True)
    copied = [p for p in (graph.mb_dir / "backups" / "B1").rglob("*") if p.is_file()]
    assert {"config.json", "doc.md"} <= {p.name for p in copied}      # the pin is not vacuous
    assert not [p for p in copied if "graph.db" in p.name or "code_graph" in p.as_posix()]
    assert not [p for p in copied if p.read_bytes()[:16] == SQLITE_HEADER]   # under any name
    assert not [k for k in manifest["files"] if "graph" in k]

    assert backup.restore(graph, "B1")["ok"]
    assert store.current_db(graph, "svc") == db and db.read_bytes() == before


# --- mb stats ------------------------------------------------------------------------------

def test_stats_count_the_graph_from_its_own_tables(graph):
    g = stats.compute(graph)["code_graph"]
    con = store.connect_ro(store.current_db(graph, "svc"))
    want = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("files", "nodes", "edges")}
    con.close()
    assert g["enabled"] is True and [u["unit"] for u in g["units"]] == ["svc"]
    assert {t: g[t] for t in want} == want and want["files"] == N_FILES and want["nodes"] == 12
    assert g["db_bytes"] >= store.current_db(graph, "svc").stat().st_size
    assert "pkg/m1.py::f1" not in json.dumps(g)          # counts, never a row's content
    line = f"code graph    : 1 graphs, {N_FILES} files, 12 nodes, {want['edges']} edges"
    assert line in render.stats(stats.compute(graph))    # also when the vault index is absent


def test_stats_output_is_unchanged_while_the_graph_was_never_switched_on(tmp_path):
    cfg = Config(vault=tmp_path / "vault", data={})
    cfg.vault.mkdir()
    s = stats.compute(cfg)
    assert s["code_graph"] == {"enabled": False, "units": [], "files": 0, "nodes": 0, "edges": 0, "db_bytes": 0}
    # both of render's branches, byte for byte as they were before the graph existed
    assert render.stats(s) == "index: absent — 0 vault notes, 0 unindexed"
    full = {**s, "present": True, "notes": 1, "chunks": 1, "avg_chunks_per_note": 1,
            "duplicate_chunks": 0, "chars_indexed": 1, "by_type": {"howto": 1}, "chunk_schema": 1}
    assert render.stats(full).splitlines() == ["notes indexed : 1 / 0 vault (0 unindexed)",
                                               "chunks        : 1 (1/note, 0 duplicate)",
                                               "text indexed  : 1 chars",
                                               "by type       : howto=1",
                                               "chunk schema  : 1"]


def test_stats_show_a_switched_off_graph_still_on_disk(graph):
    graph.data["code_graph_enabled"] = False
    assert "switched off, still on disk" in render.stats(stats.compute(graph))


# --- the import floor ----------------------------------------------------------------------

PROBE = """
import sys
from pathlib import Path
from singularity import doctor
from singularity.config import Config
from singularity.knowledge import stats
rep = doctor.Report(quiet=True)
cfg = Config(vault=Path(sys.argv[1]), data={"code_repos_dir": sys.argv[2], "code_graph_enabled": True})
doctor.code_graph_block(rep, cfg)
stats._code_graph_stats(cfg)
import singularity.codegraph.audit, singularity.machine.backup
print(rep.checks[0]["status"], sorted(m for m in ("tree_sitter", "numpy", "sentence_transformers") if m in sys.modules))
"""


def test_k2_stats_and_the_audit_module_never_import_the_parser_or_the_model(graph):
    out = subprocess.run([sys.executable, "-c", PROBE, str(graph.vault), graph.data["code_repos_dir"]],
                         capture_output=True, text=True, check=True).stdout
    assert out.strip() == "PASS []"
