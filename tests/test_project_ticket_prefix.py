"""`mb project add --ticket-prefix` / `mb project set --ticket-prefix`."""
import pytest

from singularity import cli
from singularity.config import Config


@pytest.fixture
def wired(monkeypatch, tmp_path):
    cfg = Config(vault=tmp_path / "vault", data={"projects": {
        "a": {"path": str(tmp_path / "a")}, "b": {"path": str(tmp_path / "b")}}})
    monkeypatch.setattr(cli, "load_config", lambda vault_arg=None: cfg)
    monkeypatch.setattr(Config, "save", lambda self: None)
    return cfg


def _prefix(cfg, pid):
    return cfg.data["projects"][pid].get("ticket_prefix")


def test_set_then_change(wired):
    assert cli.main(["project", "set", "a", "--ticket-prefix", "AA"]) == 0
    assert _prefix(wired, "a") == "AA"
    assert cli.main(["project", "set", "a", "--ticket-prefix", "AB"]) == 0
    assert _prefix(wired, "a") == "AB"
    assert wired.data["projects"]["a"]["path"].endswith("a")


def test_add_accepts_prefix(wired, tmp_path):
    assert cli.main(["project", "add", "c", str(tmp_path / "c"), "--ticket-prefix", "CC"]) == 0
    assert _prefix(wired, "c") == "CC"


@pytest.mark.parametrize("bad", ["a", "dd", "D1", "TOOLONGX"])
def test_bad_format_refused(wired, bad):
    assert cli.main(["project", "set", "a", "--ticket-prefix", bad]) == 1
    assert _prefix(wired, "a") is None


def test_duplicate_refused_and_unknown_project(wired, tmp_path):
    cli.main(["project", "set", "a", "--ticket-prefix", "AA"])
    assert cli.main(["project", "set", "b", "--ticket-prefix", "AA"]) == 1
    assert _prefix(wired, "b") is None
    assert cli.main(["project", "add", "c", str(tmp_path / "c"), "--ticket-prefix", "AA"]) == 1
    assert cli.main(["project", "set", "nope", "--ticket-prefix", "ZZ"]) == 1


def test_note_new_ticket_works_after_set(wired):
    assert cli.main(["note", "new", "ticket", "X", "--project", "a"]) == 1
    cli.main(["project", "set", "a", "--ticket-prefix", "AA"])
    assert cli.main(["note", "new", "ticket", "X", "--project", "a"]) == 0
    assert (wired.vault / "tickets/a/AA-1 - X.md").is_file()
