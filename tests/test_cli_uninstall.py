"""`mb uninstall` Claude hook removal (no dedicated coverage existed before
T07 amendment). Covers only what changed: _mb-marked removal (pre-existing
behavior) plus the NEW tolerant fallback for pre-marker legacy entries
recognized by their own command — [[Rule - hook ownership detection must
tolerate pre-marker installs]]. Gemini/scheduler/daemon are stubbed so this
stays a pure Claude-settings-mutation test.
"""
import json
import pathlib

import pytest

from singularity import cli
from singularity.config import Config


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda vault_arg=None: Config(vault=tmp_path, data={}))
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))

    from singularity.platform import scheduler as sched_mod
    monkeypatch.setattr(sched_mod, "unregister", lambda job: None)
    from singularity.search import client as client_mod
    monkeypatch.setattr(client_mod, "stop_daemon", lambda cfg: False)
    return tmp_path


def _settings(home):
    return home / ".claude" / "settings.json"


def _write_settings(home, hooks):
    p = _settings(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")


def test_removes_mb_marked_entries(home):
    _write_settings(home, {"Stop": [
        {"_mb": True, "hooks": [{"command": "x stop_capture.py"}]}]})
    cli.main(["uninstall"])
    data = json.loads(_settings(home).read_text(encoding="utf-8"))
    assert "Stop" not in data.get("hooks", {})


def test_removes_legacy_unmarked_own_entries_by_command(home):
    """The actual bug found on 2026-07-12: a pre-marker SessionStart/Stop
    entry (no `_mb`, but running our own script) must be recognized as
    OURS and removed, not preserved as if it were foreign."""
    _write_settings(home, {
        "SessionStart": [{"hooks": [{"command": '"...python.exe" ".../hooks/session_start.py"'}]}],
        "Stop": [{"hooks": [{"command": '"...python.exe" ".../hooks/stop_capture.py"'}]}],
    })
    cli.main(["uninstall"])
    data = json.loads(_settings(home).read_text(encoding="utf-8"))
    assert "SessionStart" not in data.get("hooks", {})
    assert "Stop" not in data.get("hooks", {})


def test_foreign_entries_preserved(home):
    _write_settings(home, {
        "Stop": [{"_mb": True, "hooks": [{"command": "x stop_capture.py"}]},
                {"hooks": [{"command": "someone-elses-stop-hook"}]}],
    })
    cli.main(["uninstall"])
    data = json.loads(_settings(home).read_text(encoding="utf-8"))
    commands = [h["hooks"][0]["command"] for h in data["hooks"]["Stop"]]
    assert commands == ["someone-elses-stop-hook"]


def test_no_settings_file_is_a_noop_not_a_crash(home):
    rc = cli.main(["uninstall"])
    assert rc == 0
