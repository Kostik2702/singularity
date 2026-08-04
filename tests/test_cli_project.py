"""T03: `mb project list|add|remove|detect` CLI wiring, atomicity, and the
malformed-config-never-overwrites guarantee (mirrors test_cli_hooks.py)."""
import json

import pytest

from singularity import cli
from singularity.config import Config


@pytest.fixture
def cfg(tmp_path):
    c = Config(vault=tmp_path, data={})
    (tmp_path / ".mb").mkdir(parents=True, exist_ok=True)
    return c


@pytest.fixture
def wired(monkeypatch, cfg):
    monkeypatch.setattr(cli, "load_config", lambda vault_arg=None: cfg)
    return cfg


def test_list_empty_registry(wired, capsys):
    assert cli.main(["project", "list"]) == 0
    assert "no projects registered" in capsys.readouterr().out


def test_add_then_list_json(wired, capsys):
    assert cli.main(["project", "add", "singularity", "D:/Singularity",
                     "--group", "core", "--about", "engine"]) == 0
    capsys.readouterr()
    cli.main(["project", "list", "--json"])
    data = json.loads(capsys.readouterr().out.strip())
    assert data["singularity"]["path"] == "D:/Singularity"
    assert data["singularity"]["group"] == "core"
    assert data["singularity"]["about"] == "engine"


def test_add_writes_atomically_and_preserves_other_keys(wired, cfg):
    cfg.data["model"] = "custom"
    cli.main(["project", "add", "p", "D:/p"])
    from singularity.config import CONFIG_FILE
    on_disk = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))   # per machine now
    assert on_disk["projects"]["p"]["path"] == "D:/p"
    assert on_disk["model"] == "custom"
    leftovers = [f for f in CONFIG_FILE.parent.iterdir()
                 if f.name.startswith(".singularity") and f.suffix != ".json"]
    assert leftovers == []


def test_add_reserved_id_rejected_without_writing(wired, cfg, capsys):
    rc = cli.main(["project", "add", "general", "D:/x"])
    assert rc == 1
    assert "reserved" in capsys.readouterr().err
    assert not (cfg.vault / ".mb" / "config.json").exists()


def test_add_duplicate_path_rejected(wired, capsys):
    cli.main(["project", "add", "a", "D:/same"])
    capsys.readouterr()
    rc = cli.main(["project", "add", "b", "D:/same"])
    assert rc == 1
    assert "already registered" in capsys.readouterr().err


def test_remove_unknown_id(wired, capsys):
    rc = cli.main(["project", "remove", "nope"])
    assert rc == 1
    assert "unknown project id" in capsys.readouterr().err


def test_remove_round_trip(wired, cfg):
    cli.main(["project", "add", "p", "D:/p"])
    assert cli.main(["project", "remove", "p"]) == 0
    from singularity.config import CONFIG_FILE
    on_disk = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))   # per machine now
    assert on_disk["projects"] == {}


def test_detect_explicit_path(wired, tmp_path, capsys):
    inner = tmp_path / "proj" / "sub"
    inner.mkdir(parents=True)
    cli.main(["project", "add", "p", str(tmp_path / "proj")])
    capsys.readouterr()
    cli.main(["project", "detect", str(inner)])
    assert capsys.readouterr().out.strip() == "p"


def test_detect_no_match_falls_back_to_general(wired, capsys, tmp_path):
    cli.main(["project", "detect", str(tmp_path / "elsewhere")])
    assert capsys.readouterr().out.strip() == "general"


def test_list_group_filters(wired, capsys):
    cli.main(["project", "add", "a", "D:/a", "--group", "g"])
    cli.main(["project", "add", "b", "D:/b"])
    capsys.readouterr()
    cli.main(["project", "list", "--group", "g", "--json"])
    data = json.loads(capsys.readouterr().out.strip())
    assert list(data) == ["a"]


def test_list_unknown_group_reports_known_ids_and_groups(wired, capsys):
    cli.main(["project", "add", "a", "D:/a", "--group", "g"])
    capsys.readouterr()
    rc = cli.main(["project", "list", "--group", "nope"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "known ids" in err and "known groups" in err


# --- malformed config never overwritten -----------------------------------------

def test_malformed_config_blocks_add_before_any_write(tmp_path, capsys):
    mb = tmp_path / ".mb"
    mb.mkdir(parents=True)
    bad = mb / "config.json"
    bad.write_text("{not json", encoding="utf-8")
    original = bad.read_text(encoding="utf-8")

    assert cli.main(["--vault", str(tmp_path), "project", "add", "p", "D:/p"]) == 2
    assert bad.read_text(encoding="utf-8") == original
