"""The installer ASKS for the vault path instead of guessing it.

Guessing is what this replaces. The engine carried one machine's absolute path
as a built-in candidate "because a last resort costs nothing"; it cost twice —
it outlived that machine's own move to another drive, and it shipped a dead
drive letter to every other user. The vault is the one thing an installer
cannot infer, so it is the one thing it asks about.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))

_spec = importlib.util.spec_from_file_location(
    "install_prompt", ENGINE / "install" / "install.py")
install = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install)


@pytest.fixture
def tty(monkeypatch):
    monkeypatch.setattr(install, "_interactive", lambda: True)


@pytest.fixture
def no_tty(monkeypatch):
    monkeypatch.setattr(install, "_interactive", lambda: False)


def _answers(monkeypatch, *replies):
    it = iter(replies)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(it))


# --- --vault wins outright ----------------------------------------------------

def test_explicit_flag_is_used_without_asking(tmp_path, tty, monkeypatch):
    vault = tmp_path / "mine"
    vault.mkdir()
    monkeypatch.setattr("builtins.input",
                        lambda _p="": pytest.fail("must not prompt with --vault"))
    assert install.choose_vault(str(vault)) == vault


def test_explicit_flag_creates_a_missing_directory(tmp_path, tty):
    """Naming a path IS the statement of where the vault should be."""
    vault = tmp_path / "brand-new"
    assert install.choose_vault(str(vault)) == vault
    assert vault.is_dir()


# --- interactive ---------------------------------------------------------------

def test_a_found_vault_is_confirmed_not_assumed(tmp_path, tty, monkeypatch):
    """A machine that already has a vault must not be silently re-pointed."""
    found = tmp_path / "found"
    found.mkdir()
    monkeypatch.setattr(install, "resolve_vault", lambda _x: found, raising=False)
    import singularity.config as config_mod
    monkeypatch.setattr(config_mod, "resolve_vault", lambda _x=None: found)
    _answers(monkeypatch, "")                       # bare Enter = accept

    assert install.choose_vault(None) == found


def test_the_offered_path_can_be_overridden(tmp_path, tty, monkeypatch):
    found = tmp_path / "found"
    found.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    import singularity.config as config_mod
    monkeypatch.setattr(config_mod, "resolve_vault", lambda _x=None: found)
    _answers(monkeypatch, str(other))

    assert install.choose_vault(None) == other


def test_nothing_configured_asks_and_creates(tmp_path, tty, monkeypatch):
    import singularity.config as config_mod
    monkeypatch.setattr(config_mod, "resolve_vault", lambda _x=None: None)
    wanted = tmp_path / "fresh vault"
    _answers(monkeypatch, str(wanted), "y")

    assert install.choose_vault(None) == wanted
    assert wanted.is_dir()


def test_quoted_answer_is_accepted(tmp_path, tty, monkeypatch):
    """People paste paths with the quotes still on."""
    import singularity.config as config_mod
    monkeypatch.setattr(config_mod, "resolve_vault", lambda _x=None: None)
    wanted = tmp_path / "quoted"
    wanted.mkdir()
    _answers(monkeypatch, f'"{wanted}"')

    assert install.choose_vault(None) == wanted


def test_declining_creation_asks_again(tmp_path, tty, monkeypatch):
    import singularity.config as config_mod
    monkeypatch.setattr(config_mod, "resolve_vault", lambda _x=None: None)
    refused = tmp_path / "no-thanks"
    accepted = tmp_path / "yes-please"
    accepted.mkdir()
    _answers(monkeypatch, str(refused), "n", str(accepted))

    assert install.choose_vault(None) == accepted
    assert not refused.exists()


# --- non-interactive -----------------------------------------------------------

def test_non_interactive_never_prompts_and_names_the_flag(tmp_path, no_tty,
                                                          monkeypatch, capsys):
    """CI, `sh -c` and scheduled runs must fail with an instruction, not hang
    on a prompt nobody can answer."""
    import singularity.config as config_mod
    monkeypatch.setattr(config_mod, "resolve_vault", lambda _x=None: None)
    monkeypatch.setattr("builtins.input",
                        lambda _p="": pytest.fail("must not prompt without a tty"))

    assert install.choose_vault(None) is None
    err = capsys.readouterr().err
    assert "--vault" in err and "non-interactive" in err


def test_non_interactive_uses_an_already_configured_vault(tmp_path, no_tty, monkeypatch):
    """A re-run on a configured machine stays silent and scriptable."""
    found = tmp_path / "configured"
    found.mkdir()
    import singularity.config as config_mod
    monkeypatch.setattr(config_mod, "resolve_vault", lambda _x=None: found)

    assert install.choose_vault(None) == found


def test_a_file_in_the_way_is_refused(tmp_path, tty, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    assert install.choose_vault(str(blocker)) is None
