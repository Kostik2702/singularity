"""`mb uninstall` Claude hook removal (no dedicated coverage existed before
T07 amendment). Covers only what changed: _mb-marked removal (pre-existing
behavior) plus the NEW tolerant fallback for pre-marker legacy entries
recognized by their own command — [[Rule - hook ownership detection must
tolerate pre-marker installs]]. Gemini/scheduler/daemon are stubbed so this
stays a pure Claude-settings-mutation test.
"""
import json
import shutil
import pathlib

import pytest

from singularity import cli
from singularity.config import Config


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda vault_arg=None: Config(vault=tmp_path, data={}))
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    # GRK-03: cmd_uninstall now also calls register_grok.uninstall(), whose
    # config root honors $GROK_HOME — a leaked developer env var would point
    # the test at a REAL Grok home. Force the isolated fallback.
    monkeypatch.delenv("GROK_HOME", raising=False)

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


# --- GRK-03: Grok owned-artifact removal ----------------------------------------

def _load_register_grok():
    import importlib.util
    engine = pathlib.Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "register_grok", engine / "install" / "register_grok.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_uninstall_removes_grok_owned_artifacts(home, monkeypatch, capsys):
    """Install -> `mb uninstall` round trip: managed AGENTS.md block and
    engine-pointing skill links removed; foreign Grok content untouched."""
    rk = _load_register_grok()
    monkeypatch.setattr(rk, "resolve_vault", lambda: home / "vault")
    monkeypatch.setattr(rk, "_grok_version", lambda: (0, 2, 111))
    monkeypatch.setattr(shutil, "which", lambda _n: None)
    (home / ".grok").mkdir()
    (home / "vault").mkdir()
    assert rk.main() == 0

    user_agents = "# my own grok notes\n"
    agents = home / ".grok" / "AGENTS.md"
    agents.write_text(user_agents + agents.read_text(encoding="utf-8"),
                      encoding="utf-8")
    foreign = home / ".grok" / "skills" / "user-own-skill"
    foreign.mkdir()
    (foreign / "SKILL.md").write_text("mine\n", encoding="utf-8")

    rc = cli.main(["uninstall"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "grok: profile removed" in out
    assert agents.read_text(encoding="utf-8") == user_agents   # user bytes intact
    assert foreign.exists()
    for name in rk.GROK_SKILLS:
        assert not (home / ".grok" / "skills" / name).exists()


def test_uninstall_reports_grok_absent_cleanly(home, capsys):
    rc = cli.main(["uninstall"])
    assert rc == 0
    assert "grok: profile absent" in capsys.readouterr().out
