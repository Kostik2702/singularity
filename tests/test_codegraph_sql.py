"""`mb code sql`: one read-only SELECT over the `v1_*` views (CODE-GRAPH P5).

The plan's acceptance, each planted: ATTACH, INSERT, PRAGMA writable_schema,
load_extension and a recursive CTE that never ends are refused -- and so are
a direct read of an internal table, a CTE that borrows a view's name to
reach one, and the very statement texts the connection prepared before the
guard went on. None of it writes a byte: every file keeps its size and
mtime and no file appears. The views: plain, joined to paths, and after an
in-place update equal to what a full build writes.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

from singularity import cli
from singularity.codegraph import build, scope, sql, store, update
from singularity.commands.code import cmd_code
from singularity.config import Config

SRC = {
    "app/__init__.py": "",
    "app/core.py": "def helper():\n    return 1\n\n\ndef run():\n    return helper()\n",
    "app/other.py": "from .core import helper\n\n\ndef use():\n    return helper()\n\n\n"
                    "def guess(x):\n    return x.run()\n",
    "tests/test_core.py": "from app.core import run\n\n\ndef test_run():\n    assert run() == 1\n",
    "web/package.json": json.dumps({"name": "@t/web"}),
    "web/a.ts": "import { b } from './b';\nexport function a(): void { b(); }\n",
    "web/b.ts": "/** The b. */\nexport function b(): void {}\n",
}

# (SQL, what the refusal names). The authorizer is asserted, not SQLite's
# read-only error: `mode=ro` alone would refuse the INSERT too, and would let
# ATTACH and VACUUM INTO create their files.
ATTACKS = [
    ("ATTACH 'stolen.db' AS s", "refused: ATTACH stolen.db"),
    ("VACUUM INTO 'copy.db'", "refused: ATTACH copy.db"),
    ("INSERT INTO files(path, lang, size, mtime_ns, content_hash, parse_status, is_test)"
     " VALUES ('x.py', 'python', 0, 0, '', 'ok', 0)", "refused: INSERT files"),
    ("INSERT INTO v1_files(path) VALUES ('x.py')", "refused: INSERT v1_files"),
    ("UPDATE nodes SET name = 'x'", "refused: UPDATE nodes"),
    ("DELETE FROM edges", "refused: DELETE edges"),
    ("PRAGMA writable_schema = 1", "refused: PRAGMA writable_schema"),
    ("SELECT load_extension('evil')", "refused: the function load_extension()"),
    ("SELECT fts3_tokenizer('simple')", "refused: the function fts3_tokenizer()"),
    ("SELECT * FROM facts", "refused: reading facts."),
    ("SELECT count(*) FROM deps", "refused: reading deps"),
    ("SELECT count(*) FROM nodes", "refused: reading nodes"),
    # a CTE borrowing a view's name gets the view's context from SQLite
    ("WITH v1_symbols AS (SELECT blob FROM facts) SELECT * FROM v1_symbols", "refused: reading facts.blob"),
    # a CTE borrowing an internal table's name, to count the real one
    ("WITH facts AS (SELECT 1) SELECT (SELECT count(*) FROM facts), count(*) FROM main.facts",
     "refused: reading facts"),
    # the exact texts `run` prepared on this connection before the guard went on
    ("SELECT k, v FROM meta", "refused: reading meta.k"),
    ("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')", "refused: reading sqlite_master.name"),
    ("SELECT value FROM json_each('[1]')", "json_each"),
    ("BEGIN IMMEDIATE", "refused: TRANSACTION BEGIN"),
    ("CREATE TEMP TABLE t(x)", "refused: INSERT sqlite_temp_master"),
    ("DROP VIEW v1_files", "refused: DELETE sqlite_master"),
    ("REINDEX", "refused: REINDEX"),
    ("SELECT 1; DELETE FROM edges", "one statement at a time"),
]
FOREVER = "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) "


@pytest.fixture(autouse=True)
def _no_background(monkeypatch):
    monkeypatch.setenv("MB_CODE_NO_BACKGROUND", "1")


def _age(root: Path) -> None:
    """Older than the racy window: a stat the graph can trust, so a refresh
    of the unchanged tree writes nothing."""
    past = time.time() - 60
    for p in root.rglob("*"):
        if p.is_file():
            os.utime(p, (past, past))


def _build(tmp_path: Path, vault: str = "vault") -> tuple[Config, scope.Unit]:
    cfg = Config(vault=tmp_path / vault, data={"code_repos_dir": str(tmp_path / "Project"),
                                               "code_graph_enabled": True})
    (unit,) = scope.discover_units(cfg)
    build.build_unit(cfg, unit, workers=1)
    return cfg, unit


@pytest.fixture
def graph(tmp_path, monkeypatch):
    root = tmp_path / "Project" / "svc"
    for rel, text in SRC.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    _age(root)
    cfg, unit = _build(tmp_path)
    monkeypatch.chdir(tmp_path)          # where ATTACH and VACUUM INTO would put their files
    return cfg, unit, root


def _run(cfg: Config, unit: scope.Unit, text: str, **kw) -> dict:
    con = store.connect_ro(store.current_db(cfg, unit.key))
    try:
        return sql.run(con, text, **kw)
    finally:
        con.close()


def _files(tmp_path: Path) -> dict[str, tuple[int, int]]:
    """Every file under the test's directory, with size and mtime. `-shm` is
    SQLite's shared-memory index, which readers touch by design."""
    return {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in tmp_path.rglob("*")
            if p.is_file() and not p.name.endswith("-shm")}


def test_a_join_across_the_views_answers_in_paths_and_qnames(graph):
    cfg, unit, _ = graph
    data = _run(cfg, unit, "SELECT e.src AS caller, s.file, f.is_test FROM v1_edges e"
                           " JOIN v1_symbols s ON s.qname = e.dst JOIN v1_files f ON f.path = e.file"
                           " WHERE e.kind = 'CALLS' AND s.name = 'helper' ORDER BY caller")
    assert data["columns"] == ["caller", "file", "is_test"]
    assert data["rows"]["items"] == [["app/core.py::run", "app/core.py", 0], ["app/other.py::use", "app/core.py", 0]]
    assert data["rows"]["has_more"] is False and data["rows"]["total"] == 2
    # a recursive walk up the callers, written by the user
    walk = _run(cfg, unit, "WITH RECURSIVE up(q, d) AS (SELECT 'app/core.py::helper', 0 UNION"
                           " SELECT e.src, d + 1 FROM v1_edges e JOIN up ON e.dst = up.q"
                           " WHERE e.kind = 'CALLS' AND d < 3) SELECT q, d FROM up WHERE d > 0 ORDER BY q")
    assert walk["rows"]["items"] == [["app/core.py::run", 1], ["app/other.py::use", 1],
                                     ["tests/test_core.py::test_run", 2]]
    # reads that take no column, a view named in capitals, a LEFT JOIN, a CTE counted
    assert _run(cfg, unit, "SELECT count(*) FROM V1_SYMBOLS")["rows"]["items"] == [[7]]
    assert _run(cfg, unit, "SELECT count(*) FROM v1_communities WHERE label IS NOT NULL")["rows"]["items"] == [[5]]
    assert _run(cfg, unit, "WITH c AS (SELECT path FROM v1_files) SELECT count(*) FROM c")["rows"]["items"] == [[6]]
    assert _run(cfg, unit, FOREVER.replace("FROM n)", "FROM n LIMIT 3)") + "SELECT count(*) FROM n"
                )["rows"]["items"] == [[3]]
    (b,) = _run(cfg, unit, "SELECT doc, line, end_line, exported FROM v1_symbols WHERE qname = 'web/b.ts::b'"
                )["rows"]["items"]
    assert b == ["The b.", 2, 2, 1]
    unplaced = _run(cfg, unit, "SELECT caller, name, receiver, file, line FROM v1_unresolved")["rows"]["items"]
    assert unplaced == [["app/other.py::guess", "run", "x", "app/other.py", 9]]


@pytest.mark.parametrize("text,named", ATTACKS, ids=[a[1].removeprefix("refused: ") for a in ATTACKS])
def test_a_planted_attack_is_refused_by_name(graph, text, named):
    cfg, unit, _ = graph
    with pytest.raises(sql.SqlRefused) as refused:
        _run(cfg, unit, text)
    assert named in str(refused.value)


def test_no_attack_writes_a_byte_or_leaves_a_file(graph, tmp_path):
    """graph.db and its WAL keep size and mtime; ATTACH and VACUUM INTO
    create no file (both do from a `mode=ro`, `query_only` connection when
    the authorizer lets them through)."""
    cfg, unit, _ = graph
    _run(cfg, unit, "SELECT 1")     # the first reader after the writer recreates an empty WAL
    before = _files(tmp_path)
    # Refusal is the other test's business: here every attack is let run its
    # course, so what is asserted is only what it left on disk.
    for text, kw in [*((a, {}) for a, _ in ATTACKS), (FOREVER + "SELECT count(*) FROM n", {"budget": 0.2})]:
        try:
            _run(cfg, unit, text, **kw)
        except sql.SqlRefused:
            pass
    assert _files(tmp_path) == before
    assert not (tmp_path / "stolen.db").exists() and not (tmp_path / "copy.db").exists()


def test_a_recursive_cte_that_never_ends_is_stopped_by_the_budget(graph):
    cfg, unit, _ = graph
    con = store.connect_ro(store.current_db(cfg, unit.key))
    # Without the budget the query would run forever: the watchdog ends it
    # instead, and the refusal then does not name the budget -- a red test,
    # not a hung one.
    watchdog = threading.Timer(20, con.interrupt)
    watchdog.start()
    started = time.monotonic()
    try:
        with pytest.raises(sql.SqlRefused, match=r"stopped after its 0\.5 s budget"):
            sql.run(con, FOREVER + "SELECT count(*) FROM n", budget=0.5)
    finally:
        watchdog.cancel()
        con.close()
    assert time.monotonic() - started < 10


def test_a_cut_answer_says_it_was_cut(graph):
    cfg, unit, _ = graph
    endless = _run(cfg, unit, FOREVER + "SELECT x FROM n", limit=5)["rows"]
    assert endless["items"] == [[1], [2], [3], [4], [5]]
    assert endless["has_more"] is True and endless["truncation_reason"] == "limit"
    assert endless["total"] is None and endless["next_offset"] is None      # unknown; the SQL pages itself
    first = _run(cfg, unit, "SELECT qname FROM v1_symbols ORDER BY qname", limit=2)["rows"]
    assert len(first["items"]) == 2 and first["has_more"] is True
    whole = _run(cfg, unit, "SELECT qname FROM v1_symbols ORDER BY qname", limit=7)["rows"]
    assert len(whole["items"]) == 7 and whole["has_more"] is False and whole["total"] == 7
    assert whole["truncation_reason"] is None


def test_a_graph_of_another_views_version_is_refused(graph):
    cfg, unit, _ = graph
    db = store.current_db(cfg, unit.key)
    for change, have in (("UPDATE meta SET v = '0' WHERE k = 'views_version'", "0"),
                         ("DELETE FROM meta WHERE k = 'views_version'", "None")):     # from before the views
        rw = sqlite3.connect(db)
        with rw:
            rw.execute(change)
        rw.close()
        with pytest.raises(sql.SqlRefused, match=rf"views are version {have} .*rebuild the graph"):
            _run(cfg, unit, "SELECT count(*) FROM v1_files")


def test_the_views_of_an_updated_graph_equal_a_full_build(graph, tmp_path):
    """update.apply writes the tables in place; plain views follow with
    nothing to refresh. Communities are left out: they are computed by the
    full build only (`mb code arch` says how far behind they are)."""
    cfg, unit, root = graph
    (root / "app" / "core.py").write_text(SRC["app/core.py"] + "\n\ndef extra():\n    return run()\n",
                                          encoding="utf-8")
    (root / "app" / "added.py").write_text("from .core import extra\n\n\ndef more():\n    return extra()\n",
                                           encoding="utf-8")
    fr = update.refresh(cfg, unit.key)
    assert sorted(fr.updated) == ["app/added.py", "app/core.py"]
    full, _ = _build(tmp_path, vault="vault-full")

    def views(c: Config) -> dict[str, list[str]]:
        return {v: sorted(json.dumps(r) for r in _run(c, unit, f"SELECT * FROM {v}", limit=10_000)["rows"]["items"])
                for v in store.VIEWS if v != "v1_communities"}

    updated = views(cfg)
    assert updated == views(full)
    assert json.dumps(["app/added.py::more", "app/core.py::extra", "CALLS", "import", "extra", "app/added.py", 5]) \
        in updated["v1_edges"]


def test_every_generation_has_the_views_plain_and_without_internal_ids(graph):
    cfg, unit, _ = graph
    for name, (_, cols, select) in store.VIEWS.items():
        assert name.startswith(f"v{store.VIEWS_VERSION}_")
        assert "UNION" not in select.upper(), f"{name}: a UNION view is materialised on every recursive step"
        assert not {c for c, _ in cols} & {"id", "file_id", "path_id", "rowid"}, name
    con = store.connect_ro(store.current_db(cfg, unit.key))
    try:
        found = {r[0]: [c[1] for c in con.execute(f"PRAGMA table_info({r[0]})")]
                 for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'view'")}
        meta = store.read_meta(con)
    finally:
        con.close()
    assert found == {name: [c for c, _ in cols] for name, (_, cols, _) in store.VIEWS.items()}
    assert meta["views_version"] == store.VIEWS_VERSION and meta["schema_version"] == store.SCHEMA_VERSION


# --- the command ---------------------------------------------------------------

def _cli(cfg: Config, capsys, *argv: str) -> tuple[int, str, str]:
    code = cmd_code(cfg, cli._build_parser().parse_args(["code", *argv]))
    out = capsys.readouterr()
    return code, out.out, out.err


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_the_command_reads_the_sql_from_a_file_or_stdin_never_argv(graph, capsys, monkeypatch, tmp_path):
    cfg, _, root = graph
    monkeypatch.chdir(root)
    query = tmp_path / "q.sql"
    query.write_text("SELECT name, line FROM v1_symbols WHERE file = 'app/core.py' ORDER BY line",
                     encoding="utf-8-sig")                     # a BOM, as Windows editors write it
    code, out, _ = _cli(cfg, capsys, "sql", "--sql-file", str(query), "--json")
    data = json.loads(out)
    assert code == 0 and data["command"] == "sql" and data["graph"]["schema_version"] == store.SCHEMA_VERSION
    assert data["columns"] == ["name", "line"] and data["rows"]["items"] == [["helper", 1], ["run", 5]]
    assert data["freshness"]["checked"] is True and data["views_version"] == store.VIEWS_VERSION

    monkeypatch.setattr(sys, "stdin", io.StringIO(query.read_text(encoding="utf-8-sig")))
    code, out, _ = _cli(cfg, capsys, "sql", "--limit", "1")
    lines = out.splitlines()
    assert code == 0 and lines[0].split() == ["name", "line"] and lines[2].split() == ["helper", "1"]
    assert lines[-1].startswith("… more rows past --limit 1")

    # a real pipe: bytes, read as UTF-8 whatever the console's code page, a BOM dropped
    piped = "SELECT 'наивный ü' AS said".encode("utf-8-sig")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(piped), encoding="cp1251"))
    code, out, _ = _cli(cfg, capsys, "sql", "--json")
    assert code == 0 and json.loads(out)["rows"]["items"] == [["наивный ü"]]

    monkeypatch.setattr(sys, "stdin", io.StringIO("DELETE FROM edges"))
    code, _, err = _cli(cfg, capsys, "sql")
    assert code == 2 and "refused: DELETE edges" in err
    monkeypatch.setattr(sys, "stdin", _Tty())
    code, _, err = _cli(cfg, capsys, "sql")
    assert code == 2 and "--sql-file" in err                  # a terminal is not waited on
    code, _, err = _cli(cfg, capsys, "sql", "--sql-file", str(tmp_path / "missing.sql"))
    assert code == 2 and "cannot read the SQL" in err
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["code", "sql", "SELECT 1"])


def test_schema_lists_the_views_and_their_columns(graph, capsys, monkeypatch):
    cfg, _, root = graph
    monkeypatch.chdir(root)
    code, out, _ = _cli(cfg, capsys, "schema", "--no-refresh")
    assert code == 0
    for name, (_, cols, _) in store.VIEWS.items():
        assert f"  {name}: " in out
        assert all(f"    {c} " in out for c, _ in cols)
    _, out, _ = _cli(cfg, capsys, "schema", "--json", "--no-refresh")
    data = json.loads(out)
    assert data["views_version"] == store.VIEWS_VERSION
    assert list(data["views"]["v1_edges"]["columns"]) == ["src", "dst", "kind", "tier", "callee", "file", "line"]
