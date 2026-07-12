"""T11: `mb digest plan|mark` CLI wiring (mirrors test_cli_project.py)."""
import json

import numpy as np
import pytest

from singularity import cli
from singularity.config import Config
from singularity.search import indexer


@pytest.fixture
def cfg(tmp_path):
    c = Config(vault=tmp_path, data={})
    (tmp_path / ".mb").mkdir(parents=True, exist_ok=True)
    return c


@pytest.fixture
def wired(monkeypatch, cfg):
    monkeypatch.setattr(cli, "load_config", lambda vault_arg=None: cfg)
    return cfg


def _commit(cfg, pairs):
    chunks = [c for c, _ in pairs]
    embs = np.array([v for _, v in pairs], dtype=np.float32)
    indexer._commit_generation(
        cfg.memory_index_dir, embs,
        {"model": "m", "file_meta": {}, "chunks": chunks})


def _chunk(file, section, type_=None, project=None):
    c = {"file": file, "section": section, "title": section}
    if type_ is not None:
        c["type"] = type_
    if project is not None:
        c["project"] = project
    return c


def test_plan_no_index_reports_missing(wired, capsys):
    rc = cli.main(["digest", "plan"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "missing" in out
    assert "no candidates" in out


def test_plan_json_shape(wired, cfg, capsys):
    _commit(cfg, [
        (_chunk("learnings/active/T.md", "S1", "learning"), [1, 0]),
        (_chunk("reference/R.md", "R1", "reference"), [1, 0]),
    ])
    rc = cli.main(["digest", "plan", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out.strip())
    assert data["index_status"] == "ok"
    assert data["entries"][0]["candidates"][0]["file"] == "reference/R.md"


def test_plan_project_filter_unknown_token_errors(wired, capsys):
    rc = cli.main(["digest", "plan", "--project", "nope"])
    assert rc == 1
    assert "unknown project" in capsys.readouterr().err


def test_mark_one_identity(wired, cfg, capsys):
    _commit(cfg, [(_chunk("learnings/active/T.md", "S1", "learning"), [1, 0])])
    rc = cli.main(["digest", "mark", "learnings/active/T.md", "S1"])
    assert rc == 0
    assert "marked: 1" in capsys.readouterr().out
    data = json.loads((cfg.mb_dir / "digest.json").read_text(encoding="utf-8"))
    assert "learnings/active/T.md#S1" in data["marked"]


def test_mark_all_marks_every_current_entry(wired, cfg, capsys):
    _commit(cfg, [
        (_chunk("learnings/active/A.md", "A", "learning"), [1, 0]),
        (_chunk("learnings/active/B.md", "B", "learning"), [0, 1]),
    ])
    rc = cli.main(["digest", "mark", "--all"])
    assert rc == 0
    assert "marked: 2" in capsys.readouterr().out
    capsys.readouterr()
    cli.main(["digest", "plan", "--json"])
    data = json.loads(capsys.readouterr().out.strip())
    assert data["entries"] == []


def test_mark_without_args_or_all_errors(wired, capsys):
    rc = cli.main(["digest", "mark"])
    assert rc == 1
    assert "usage" in capsys.readouterr().err
