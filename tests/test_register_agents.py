"""Unit tests for install/register_agents — engine-owned agent deployer (AUD-12).

Mirrors the register_skills safety contract for single-file agents: identical
files are replaced/left alone, differing user files are backed up (never lost),
only owned names are touched.
"""
import importlib.util
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "register_agents", ENGINE / "install" / "register_agents.py")
register_agents = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(register_agents)


def test_engine_ships_all_owned_agents():
    for name in register_agents.AGENTS:
        assert (register_agents.AGENTS_SRC / f"{name}.md").is_file(), name


def test_registry_completeness_matches_source_dir():
    # Everything the installer deploys must equal the .md files in the engine
    # source dir — agents plus the shared preamble, which ships with them so the
    # rules a subagent starts on never depend on a vault being there.
    on_disk = sorted(p.stem for p in register_agents.AGENTS_SRC.glob("*.md"))
    assert sorted(register_agents.AGENTS + [register_agents.PREAMBLE]) == on_disk
    assert register_agents.PREAMBLE not in register_agents.AGENTS, \
        "the preamble is deployed alongside the agents, it is not one of them"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(register_agents.Path, "home",
                        classmethod(lambda cls: tmp_path))
    return tmp_path


def test_fresh_install_deploys_all(home):
    rc = register_agents.main()
    assert rc == 0
    dest = home / ".claude" / "agents"
    for name in register_agents.AGENTS:
        assert (dest / f"{name}.md").exists()


def test_idempotent_rerun(home):
    register_agents.main()
    rc = register_agents.main()          # second run is a no-op, still success
    assert rc == 0


def test_identical_local_file_not_backed_up(home):
    dest = home / ".claude" / "agents"
    dest.mkdir(parents=True)
    name = register_agents.AGENTS[0]
    src = register_agents.AGENTS_SRC / f"{name}.md"
    (dest / f"{name}.md").write_bytes(src.read_bytes())   # byte-identical
    register_agents.main()
    backups = list(dest.glob(f"{name}.md.backup-*"))
    assert backups == []                                  # nothing to preserve


def test_differing_local_file_is_backed_up_not_lost(home):
    dest = home / ".claude" / "agents"
    dest.mkdir(parents=True)
    name = register_agents.AGENTS[0]
    (dest / f"{name}.md").write_text("MY LOCAL EDITS", encoding="utf-8")
    register_agents.main()
    backups = list(dest.glob(f"{name}.md.backup-*"))
    backups = [b for b in backups if not b.name.endswith(".manifest.txt")]
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "MY LOCAL EDITS"   # preserved


def test_unowned_agent_left_untouched(home):
    dest = home / ".claude" / "agents"
    dest.mkdir(parents=True)
    (dest / "comfyui-scout.md").write_text("foreign agent", encoding="utf-8")
    register_agents.main()
    assert (dest / "comfyui-scout.md").read_text(encoding="utf-8") == "foreign agent"
