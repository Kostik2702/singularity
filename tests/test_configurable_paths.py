"""Machine-specific literals become defaults, not requirements.

The engine carried two hardcoded paths belonging to one machine: the built-in
fallback vault, and the vault path written into the agent definitions. Neither
is deleted — both keep working as the LAST fallback — but neither is the only
possibility any more.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from singularity import config as config_mod

# `install/` is not an importable package — load the module by path, the same
# way tests/test_register_agents.py already does.
ENGINE = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "register_agents_cfgpaths", ENGINE / "install" / "register_agents.py")
register_agents = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(register_agents)


# --- built-in vault candidates ----------------------------------------------

def test_candidates_are_home_relative_before_the_machine_literal(monkeypatch):
    monkeypatch.delenv("MB_DEFAULT_VAULT", raising=False)
    cands = config_mod.default_vault_candidates()
    assert cands[-1] == Path(config_mod.DEFAULT_VAULT_WINDOWS)   # still last
    assert all(Path.home() in c.parents for c in cands[:-1])


def test_env_replaces_the_whole_candidate_list(monkeypatch, tmp_path):
    import os
    monkeypatch.setenv("MB_DEFAULT_VAULT",
                       os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")]))
    assert config_mod.default_vault_candidates() == [tmp_path / "a", tmp_path / "b"]


def test_env_candidates_expand_home(monkeypatch):
    monkeypatch.setenv("MB_DEFAULT_VAULT", "~/vaults/mine")
    assert config_mod.default_vault_candidates() == [Path.home() / "vaults" / "mine"]


def test_resolve_uses_the_first_candidate_that_exists(monkeypatch, tmp_path):
    import os
    real = tmp_path / "real"
    real.mkdir()
    monkeypatch.delenv("MB_HOME", raising=False)
    monkeypatch.setattr(config_mod, "POINTER_FILE", tmp_path / "absent.json")
    monkeypatch.setenv("MB_DEFAULT_VAULT",
                       os.pathsep.join([str(tmp_path / "missing"), str(real)]))
    assert config_mod.resolve_vault() == real


def test_no_candidate_exists_still_returns_none(monkeypatch, tmp_path):
    monkeypatch.delenv("MB_HOME", raising=False)
    monkeypatch.setattr(config_mod, "POINTER_FILE", tmp_path / "absent.json")
    monkeypatch.setenv("MB_DEFAULT_VAULT", str(tmp_path / "nope"))
    assert config_mod.resolve_vault() is None


@pytest.mark.parametrize("source", ["explicit", "env", "pointer"])
def test_configured_sources_still_win_over_the_defaults(monkeypatch, tmp_path, source):
    """The candidate list is a LAST resort; it must never shadow a real answer."""
    import json
    wanted = tmp_path / "wanted"
    wanted.mkdir()
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    monkeypatch.setenv("MB_DEFAULT_VAULT", str(decoy))
    monkeypatch.delenv("MB_HOME", raising=False)
    pointer = tmp_path / "pointer.json"
    monkeypatch.setattr(config_mod, "POINTER_FILE", pointer)

    if source == "explicit":
        assert config_mod.resolve_vault(str(wanted)) == wanted
        return
    if source == "env":
        monkeypatch.setenv("MB_HOME", str(wanted))
    else:
        pointer.write_text(json.dumps({"vault_path": str(wanted)}), encoding="utf-8")
    assert config_mod.resolve_vault() == wanted


# --- agent definitions -------------------------------------------------------

def test_agent_sources_still_carry_the_template_path_verbatim():
    """The literal is the template default and must stay greppable — the
    installer's substitution keys off exactly this string."""
    src = register_agents.AGENTS_SRC / "investigator.md"
    assert register_agents.TEMPLATE_VAULT in src.read_text(encoding="utf-8")


def test_installer_renders_the_real_vault_when_it_differs(tmp_path, monkeypatch):
    vault = tmp_path / "elsewhere" / "brain"
    vault.mkdir(parents=True)
    home = tmp_path / "home"
    (home / ".claude" / "agents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(register_agents, "_resolved_vault", lambda: vault)

    assert register_agents.main() == 0
    rendered = (home / ".claude" / "agents" / "investigator.md").read_text(
        encoding="utf-8")
    assert str(vault) in rendered
    assert register_agents.TEMPLATE_VAULT not in rendered


def test_installer_links_verbatim_when_the_vault_matches_the_template(
        tmp_path, monkeypatch):
    """The historic behaviour — and the property that matters: edit the repo,
    every deployed agent follows without re-running anything."""
    home = tmp_path / "home"
    (home / ".claude" / "agents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(register_agents, "_resolved_vault",
                        lambda: Path(register_agents.TEMPLATE_VAULT))
    calls = []
    real_install = register_agents._install_file
    monkeypatch.setattr(register_agents, "_install_file",
                        lambda s, d: calls.append(s.name) or real_install(s, d))

    assert register_agents.main() == 0
    assert len(calls) == len(register_agents.AGENTS)   # every one took the link path


def test_rendering_is_idempotent(tmp_path, monkeypatch):
    vault = tmp_path / "brain"
    vault.mkdir()
    home = tmp_path / "home"
    (home / ".claude" / "agents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(register_agents, "_resolved_vault", lambda: vault)

    register_agents.main()
    first = (home / ".claude" / "agents" / "coder.md").read_text(encoding="utf-8")
    register_agents.main()
    assert (home / ".claude" / "agents" / "coder.md").read_text(
        encoding="utf-8") == first
    # no backup churn on a byte-identical rerun
    assert not list((home / ".claude" / "agents").glob("*.backup-*"))


def test_a_users_own_edit_is_backed_up_never_overwritten(tmp_path, monkeypatch):
    vault = tmp_path / "brain"
    vault.mkdir()
    home = tmp_path / "home"
    agents = home / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "critic.md").write_text("my own agent, do not touch",
                                      encoding="utf-8")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(register_agents, "_resolved_vault", lambda: vault)

    register_agents.main()
    # `_backup_file` writes a `.manifest.txt` sidecar next to each backup
    backups = [p for p in agents.glob("critic.md.backup-*")
               if not p.name.endswith(".manifest.txt")]
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "my own agent, do not touch"
    assert str(vault) in (agents / "critic.md").read_text(encoding="utf-8")


def test_unresolvable_vault_falls_back_to_linking(tmp_path, monkeypatch):
    """A checkout with no vault configured must still deploy agents."""
    home = tmp_path / "home"
    (home / ".claude" / "agents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(register_agents, "_resolved_vault", lambda: None)
    assert register_agents.main() == 0
    assert (home / ".claude" / "agents" / "coder.md").exists()
