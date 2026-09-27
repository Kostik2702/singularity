"""No machine-specific literal is a default any more.

The engine carried two hardcoded paths belonging to one machine: the built-in
fallback vault and the vault path written into the agent definitions. Keeping
them "as the last fallback" was the earlier compromise, and it failed twice:
the fallback outlived that machine's own move to another drive, and it shipped
a dead drive letter to everyone else. Now the installer asks, and the agent /
skill sources carry a token rather than a path.
"""
import importlib.util
import re
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

def test_every_builtin_candidate_is_home_relative(monkeypatch):
    """No default may name a drive or a machine.

    The list used to end with one machine's absolute path. It outlived that
    machine's own move to another drive and shipped a dead drive letter to
    everybody else — a default nobody can satisfy is not a fallback."""
    monkeypatch.delenv("MB_DEFAULT_VAULT", raising=False)
    cands = config_mod.default_vault_candidates()
    assert cands, "there must still be a home-relative guess"
    assert all(Path.home() in c.parents for c in cands)


def test_no_drive_letter_default_survives_anywhere_in_config():
    """Structural, not just behavioural: the constant is gone, so it cannot be
    reintroduced by a well-meant 'last resort costs nothing' edit."""
    src = (ENGINE / "singularity" / "config.py").read_text(encoding="utf-8")
    assert "DEFAULT_VAULT_WINDOWS" not in src
    assert not re.search(r'r?"[A-Za-z]:\\\\?[A-Za-z]', src), \
        "config.py must not contain an absolute drive path"


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

def test_agent_sources_name_paths_only_through_tokens():
    """Sources must never bake in a machine path — that is what went stale when
    the authoring machine moved drives. Both tokens stay greppable: the
    installer's substitution keys off exactly these strings.

    `{{VAULT}}` now appears only where a VAULT path is genuinely meant (optional
    knowledge an agent may consult). The shared preamble moved to `{{AGENTS}}`
    deliberately: it is the rule set every subagent starts on, and a mechanism
    may not depend on which vault — or whether a vault — is configured."""
    import vault_token

    sources = {p.name: p.read_text(encoding="utf-8")
               for p in register_agents.AGENTS_SRC.glob("*.md")}
    assert sources, "no agent sources found"

    for name, text in sources.items():
        assert "/Users/" not in text and "C:\\" not in text and "E:\\" not in text, \
            f"{name} bakes in an absolute machine path instead of a token"

    assert any(register_agents.TEMPLATE_VAULT in t for t in sources.values()), \
        "no source names {{VAULT}} any more — substitution would be untested"
    assert any(vault_token.AGENTS_TOKEN in t for t in sources.values()), \
        "no source names {{AGENTS}} — the preamble would be addressed elsewhere"


def test_installer_renders_the_real_vault_when_it_differs(tmp_path, monkeypatch):
    vault = tmp_path / "elsewhere" / "brain"
    vault.mkdir(parents=True)
    home = tmp_path / "home"
    (home / ".claude" / "agents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(register_agents, "_resolved_vault", lambda: vault)

    assert register_agents.main() == 0
    # coder.md is one of the sources that still names a vault path (optional
    # project rules); investigator.md addresses only the engine-shipped preamble.
    rendered = (home / ".claude" / "agents" / "coder.md").read_text(encoding="utf-8")
    assert str(vault) in rendered
    assert register_agents.TEMPLATE_VAULT not in rendered

    # and the mechanism half: the preamble is addressed where it is deployed,
    # not through the vault
    every = (home / ".claude" / "agents" / "investigator.md").read_text(encoding="utf-8")
    assert (home / ".claude" / "agents" / "AGENT_SHARED_PREAMBLE.md").as_posix() in every
    assert (home / ".claude" / "agents" / "AGENT_SHARED_PREAMBLE.md").exists(), \
        "agents point at a preamble the installer did not deploy"


def test_a_source_without_tokens_is_linked_so_repo_edits_take_effect(
        tmp_path, monkeypatch):
    """The property that matters: edit the repo, the deployed copy follows with
    no re-install. A file naming no token needs no substitution, so it is
    symlinked — and the shared preamble is exactly that file, which is why
    editing the rules every subagent runs on does not require an installer run.

    Sources that DO name a token must be rendered instead; a symlinked
    `{{AGENTS}}` would send agents to a file called literally "{{AGENTS}}"."""
    home = tmp_path / "home"
    (home / ".claude" / "agents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(register_agents, "_resolved_vault",
                        lambda: Path(register_agents.TEMPLATE_VAULT))
    linked = []
    real_install = register_agents._install_file
    monkeypatch.setattr(register_agents, "_install_file",
                        lambda s, d: linked.append(s.name) or real_install(s, d))

    assert register_agents.main() == 0
    assert linked == [f"{register_agents.PREAMBLE}.md"], \
        "only the token-free preamble may take the link path"
    for name in register_agents.AGENTS:
        deployed = (home / ".claude" / "agents" / f"{name}.md").read_text(encoding="utf-8")
        assert "{{AGENTS}}" not in deployed, f"{name} deployed with an unresolved token"


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
    # and the replacement is a real rendering for this machine, not the template
    written = (agents / "critic.md").read_text(encoding="utf-8")
    assert (agents / "AGENT_SHARED_PREAMBLE.md").as_posix() in written
    assert "{{" not in written


def test_unresolvable_vault_falls_back_to_linking(tmp_path, monkeypatch):
    """A checkout with no vault configured must still deploy agents."""
    home = tmp_path / "home"
    (home / ".claude" / "agents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(register_agents, "_resolved_vault", lambda: None)
    assert register_agents.main() == 0
    assert (home / ".claude" / "agents" / "coder.md").exists()
