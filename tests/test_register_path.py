"""PATH registration (issue #15): a successful install left no `mb` on PATH.

The registrar is the only place that writes outside the engine on behalf of
this feature, so its ownership rule gets the same scrutiny as skills and
hooks: create/refresh what points back into THIS engine, never touch anything
else, and remove exactly what we created.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))

_spec = importlib.util.spec_from_file_location(
    "register_path", ENGINE / "install" / "register_path.py")
register_path = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(register_path)

posix_only = pytest.mark.skipif(os.name == "nt",
                                reason="POSIX symlink strategy (Windows uses the registry)")


@pytest.fixture
def fake_engine(tmp_path, monkeypatch):
    """A throwaway engine with a venv full of console scripts."""
    engine = tmp_path / "engine"
    bin_dir = engine / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    for tool in ("mb", "imgqa"):
        script = bin_dir / tool
        script.write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
        script.chmod(0o755)
    link_dir = tmp_path / "home" / ".local" / "bin"
    monkeypatch.setattr(register_path, "ENGINE", engine)
    monkeypatch.setattr(register_path, "POSIX_LINK_DIR", link_dir)
    monkeypatch.setattr(register_path, "console_tools", lambda: ["mb", "imgqa"])
    return engine, bin_dir, link_dir


# --- what to register --------------------------------------------------------

def test_console_tools_come_from_pyproject():
    """Hardcoding the list is how it drifts; `reel`-style additions must be
    picked up without touching this file."""
    tools = register_path.console_tools()
    assert "mb" in tools and "imgqa" in tools
    declared = (ENGINE / "pyproject.toml").read_text(encoding="utf-8")
    for tool in tools:
        assert f"\n{tool} = " in declared


def test_venv_bin_dir_finds_the_real_engine_venv():
    d = register_path.venv_bin_dir()
    if d is not None:
        assert d.name in ("bin", "Scripts")
        assert d.is_dir()


# --- POSIX symlinks ----------------------------------------------------------

@posix_only
def test_register_links_every_console_script(fake_engine):
    _engine, bin_dir, link_dir = fake_engine
    res = register_path._posix_register(bin_dir)
    assert sorted(res["linked"]) == ["imgqa", "mb"]
    assert (link_dir / "mb").resolve() == (bin_dir / "mb").resolve()


@posix_only
def test_register_is_idempotent(fake_engine):
    _engine, bin_dir, _link = fake_engine
    register_path._posix_register(bin_dir)
    second = register_path._posix_register(bin_dir)
    assert second["linked"] == [] and sorted(second["ok"]) == ["imgqa", "mb"]


@posix_only
def test_register_never_clobbers_a_foreign_file(fake_engine):
    """Someone else's ~/.local/bin/mb is not ours to delete."""
    _engine, bin_dir, link_dir = fake_engine
    link_dir.mkdir(parents=True)
    foreign = link_dir / "mb"
    foreign.write_text("#!/bin/sh\necho someone else\n", encoding="utf-8")

    res = register_path._posix_register(bin_dir)

    assert res["foreign"] == ["mb"]
    assert "someone else" in foreign.read_text(encoding="utf-8")
    assert not foreign.is_symlink()


@posix_only
def test_uninstall_removes_only_our_links(fake_engine):
    _engine, bin_dir, link_dir = fake_engine
    register_path._posix_register(bin_dir)
    foreign_target = link_dir.parent / "other-mb"
    foreign_target.write_text("#!/bin/sh\n", encoding="utf-8")
    (link_dir / "imgqa").unlink()
    (link_dir / "imgqa").symlink_to(foreign_target)     # another install's link

    res = register_path._posix_uninstall()

    assert res["removed"] == ["mb"] and res["kept"] == ["imgqa"]
    assert not (link_dir / "mb").exists()
    assert (link_dir / "imgqa").is_symlink()


@posix_only
def test_register_reports_scripts_pip_never_generated(fake_engine):
    _engine, bin_dir, _link = fake_engine
    (bin_dir / "imgqa").unlink()
    res = register_path._posix_register(bin_dir)
    assert res["missing"] == ["imgqa"]


# --- Windows user PATH (runs everywhere: the registry I/O is stubbed) --------

@pytest.fixture
def fake_registry(monkeypatch):
    """Stand in for HKCU\\Environment. `_win_read_path`/`_win_write_path` are
    the only two functions that touch it, which is also what lets conftest
    guard the real one."""
    state = {"value": r"C:\Windows;C:\Windows\System32", "type": 2, "writes": 0}
    monkeypatch.setattr(register_path, "_win_read_path",
                        lambda: (state["value"], state["type"]))

    def write(value, vtype):
        state["value"], state["type"], state["writes"] = value, vtype, state["writes"] + 1

    monkeypatch.setattr(register_path, "_win_write_path", write)
    return state


def test_win_register_appends_the_venv_scripts_dir(fake_registry, tmp_path):
    res = register_path._win_register(tmp_path / "Scripts")
    assert res["path_entry"] == "added"
    assert fake_registry["value"].endswith(str(tmp_path / "Scripts"))
    assert fake_registry["value"].startswith(r"C:\Windows;")     # nothing lost


def test_win_register_is_idempotent(fake_registry, tmp_path):
    scripts = tmp_path / "Scripts"
    register_path._win_register(scripts)
    before = fake_registry["writes"]
    res = register_path._win_register(scripts)
    assert res["path_entry"] == "already present"
    assert fake_registry["writes"] == before        # no second write


def test_win_register_preserves_the_value_type(fake_registry, tmp_path):
    """A REG_EXPAND_SZ PATH containing %USERPROFILE% must not be flattened to
    REG_SZ — that is the corruption `setx` is famous for."""
    fake_registry["value"] = r"%USERPROFILE%\bin"
    fake_registry["type"] = 2                        # REG_EXPAND_SZ
    register_path._win_register(tmp_path / "Scripts")
    assert fake_registry["type"] == 2
    assert r"%USERPROFILE%\bin" in fake_registry["value"]


def test_win_uninstall_removes_only_our_entry(fake_registry, tmp_path):
    scripts = tmp_path / "Scripts"
    register_path._win_register(scripts)
    res = register_path._win_uninstall(scripts)
    assert res["path_entry"] == "removed"
    assert str(scripts) not in fake_registry["value"]
    assert r"C:\Windows\System32" in fake_registry["value"]


def test_win_uninstall_writes_nothing_when_we_are_not_there(fake_registry, tmp_path):
    before = fake_registry["writes"]
    res = register_path._win_uninstall(tmp_path / "Scripts")
    assert res["path_entry"] == "not present"
    assert fake_registry["writes"] == before


# --- status (what doctor's D7 reads) ----------------------------------------

def test_status_flags_an_mb_from_another_clone(monkeypatch, tmp_path):
    other = tmp_path / "other-clone" / ".venv" / "bin" / "mb"
    other.parent.mkdir(parents=True)
    other.write_text("", encoding="utf-8")
    monkeypatch.setattr(register_path.shutil, "which", lambda name: str(other))

    st = register_path.status()

    assert st["on_path"] is True
    assert st["is_ours"] is False


def test_status_reports_absent_mb(monkeypatch):
    monkeypatch.setattr(register_path.shutil, "which", lambda name: None)
    st = register_path.status()
    assert st["on_path"] is False and st["is_ours"] is False


def test_status_recognises_this_engines_own_mb(monkeypatch):
    ours = ENGINE / ".venv" / "bin" / "mb"
    monkeypatch.setattr(register_path.shutil, "which", lambda name: str(ours))
    assert register_path.status()["is_ours"] is True


# --- the shell hint ----------------------------------------------------------

@pytest.mark.parametrize("shell,expected", [
    ("/bin/zsh", "~/.zshrc"),
    ("/bin/bash", "~/.bashrc"),
    ("/usr/bin/fish", "fish_add_path"),
])
def test_shell_hint_names_the_users_rc_file(monkeypatch, shell, expected):
    """A hint that tells someone to edit the wrong file is worse than none."""
    monkeypatch.setenv("SHELL", shell)
    assert expected in register_path._shell_hint(Path("/home/u/.local/bin"))
