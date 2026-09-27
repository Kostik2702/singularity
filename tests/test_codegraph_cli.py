"""`mb code` verbs and the answer envelope (CODE-GRAPH P2).

Planted each time: the switch that is off, a list longer than the limit that
must say it was cut, a name two definitions share, a file edited after the
build whose stored spans no longer fit, and an import of the parser on a
path that only reads.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from singularity import cli
from singularity.codegraph import build, scope
from singularity.commands.code import cmd_code
from singularity.config import Config

SRC = {
    "app/__init__.py": "",
    "app/core.py": "def helper():\n    return 1\n\n\ndef run():\n    return helper()\n\n\n"
                   "def main():\n    run()\n    return helper()\n",
    "app/other.py": "from .core import helper\n\n\ndef use():\n    return helper()\n\n\n"
                    "def run():\n    return 0\n\n\ndef guess(x):\n    return x.run()\n",
}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "Project" / "svc"
    for rel, text in SRC.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    cfg = Config(vault=tmp_path / "vault",
                 data={"code_repos_dir": str(tmp_path / "Project"), "code_graph_enabled": True})
    (unit,) = scope.discover_units(cfg)
    build.build_unit(cfg, unit)
    monkeypatch.chdir(root)
    return cfg, root


def _run(cfg, capsys, *argv):
    args = cli._build_parser().parse_args(["code", *argv])
    code = cmd_code(cfg, args)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_every_verb_but_status_refuses_while_the_graph_is_off(repo, capsys):
    cfg, _ = repo
    cfg.data["code_graph_enabled"] = False
    code, _, err = _run(cfg, capsys, "callers", "helper")
    assert code == 2 and "code_graph_enabled" in err
    code, out, _ = _run(cfg, capsys, "status", "--json")
    assert code == 0 and json.loads(out)["enabled"] is False


def test_callers_carry_tiers_candidates_and_an_honest_envelope(repo, capsys):
    cfg, _ = repo
    code, out, _ = _run(cfg, capsys, "callers", "app/core.py::helper", "--json")
    data = json.loads(out)
    assert code == 0
    assert data["schema_version"] == 1 and data["graph"]["unit"] == "svc"
    assert data["freshness"]["checked"] is True and data["freshness"]["stale_count"] == 0
    _, out, _ = _run(cfg, capsys, "callers", "app/core.py::helper", "--json", "--no-refresh")
    assert json.loads(out)["freshness"]["checked"] is False     # not compared, and says so
    callers = {(c["caller"], c["tier"]) for c in data["callers"]["items"]}
    assert ("app/core.py::run", "scope") in callers and ("app/other.py::use", "import") in callers
    # `x.run()` on an untyped x with two `run`s defined: not an edge, but a
    # candidate every answer about either `run` names
    _, out, _ = _run(cfg, capsys, "callers", "app/core.py::run", "--json")
    run = json.loads(out)
    assert [c["caller"] for c in run["callers"]["items"]] == ["app/core.py::main"]
    assert any(c["caller"] == "app/other.py::guess" for c in run["unresolved_candidates"]["items"])


def test_a_cut_list_says_it_was_cut(repo, capsys):
    cfg, _ = repo
    _, out, _ = _run(cfg, capsys, "callers", "app/core.py::helper", "--limit", "1", "--json")
    page = json.loads(out)["callers"]
    assert len(page["items"]) == 1 and page["has_more"] and page["next_offset"] == 1
    assert page["truncation_reason"] == "limit"


def test_a_shared_name_is_refused_with_the_candidates(repo, capsys):
    cfg, _ = repo
    code, _, err = _run(cfg, capsys, "callers", "run")
    assert code == 2 and "app/core.py::run" in err and "app/other.py::run" in err


def test_a_snippet_of_an_edited_file_is_refused_not_mis_cut(repo, capsys):
    """Without the refresh the stored spans point at the old lines: refused.
    With it (the default) the file is updated first and cut right."""
    cfg, root = repo
    code, out, _ = _run(cfg, capsys, "snippet", "app/core.py::run")
    assert code == 0 and "return helper()" in out
    (root / "app" / "core.py").write_text("# moved\n" + SRC["app/core.py"], encoding="utf-8")
    code, _, err = _run(cfg, capsys, "snippet", "app/core.py::run", "--no-refresh")
    assert code == 2 and "changed since the graph was built" in err
    code, out, err = _run(cfg, capsys, "snippet", "app/core.py::run", "--json")
    node = json.loads(out)["symbol"]
    assert code == 0 and node["spans"][0][0] == 6 and "return helper()" in node["code"][0]
    assert "updated 1 changed file" not in err                  # --json: the note is in the envelope
    assert json.loads(out)["freshness"]["updated"] == ["app/core.py"]


def test_trace_outline_find_and_schema_answer(repo, capsys):
    cfg, _ = repo
    _, out, _ = _run(cfg, capsys, "trace", "app/core.py::helper", "--depth", "2", "--json")
    reached = {r["symbol"]: r["depth"] for r in json.loads(out)["reached"]["items"]}
    assert reached["app/core.py::run"] == 1 and reached["app/core.py::main"] == 1
    _, out, _ = _run(cfg, capsys, "outline", "app/core.py", "--json")
    assert [s["name"] for s in json.loads(out)["symbols"]] == ["helper", "run", "main"]
    _, out, _ = _run(cfg, capsys, "find", "help", "--json")
    assert json.loads(out)["matches"]["total"] == 1
    _, out, _ = _run(cfg, capsys, "schema", "--json")
    assert json.loads(out)["edges"]["CALLS"] >= 4


def test_a_directory_outside_every_built_unit_gets_a_hint(repo, capsys, tmp_path, monkeypatch):
    cfg, _ = repo
    monkeypatch.chdir(tmp_path)
    code, _, err = _run(cfg, capsys, "callers", "helper")
    assert code == 2 and "mb code build" in err


def test_the_query_path_never_imports_the_parser_or_the_model():
    """B2's floor: an answer is read from SQLite. If the read path pulls in
    tree-sitter, numpy or sentence-transformers it pays their import on every
    call -- and fails outright on an interpreter without the parser."""
    probe = ("import sys\n"
             "import singularity.cli, singularity.commands.code, singularity.codegraph.query\n"
             "print(sorted(m for m in ('tree_sitter', 'numpy', 'sentence_transformers') if m in sys.modules))\n")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"
