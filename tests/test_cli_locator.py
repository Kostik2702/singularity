"""Finding a Node CLI that PATH alone cannot see (issue #10).

nvm is a shell function sourced from an interactive rc file, not a binary on
the system path, so `shutil.which("gemini")` answers None inside an installer,
a CI job, a launchd/systemd job or anything under `sudo` — while the very same
machine has `@google/gemini-cli@0.53.1` installed and working. Presence was
then detected by directory and version by `which`, and the mismatch was
silently downgraded to "hooks skipped: requires gemini >= 0.26.0".
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))

_spec = importlib.util.spec_from_file_location(
    "cli_locator", ENGINE / "install" / "cli_locator.py")
cli_locator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli_locator)

EXE = "tool.cmd" if os.name == "nt" else "tool"


def _make_exe(directory: Path, name: str = EXE) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / name
    p.write_text("@echo off\n" if name.endswith(".cmd") else "#!/bin/sh\n",
                 encoding="utf-8")
    p.chmod(0o755)
    return p


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """No inherited NVM_*/FNM_* from the developer's shell, and a home that
    holds nothing, so every test states its own world."""
    for var in ("NVM_BIN", "NVM_DIR", "FNM_MULTISHELL_PATH"):
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    return home


def test_path_wins_when_the_cli_is_visible(tmp_path, monkeypatch):
    on_path = tmp_path / "bin"
    _make_exe(on_path)
    monkeypatch.setenv("PATH", str(on_path))
    found, where = cli_locator.find_cli("tool")
    assert found and Path(found).parent == on_path
    assert where == "PATH"


def test_absent_everywhere_is_reported_as_absent():
    assert cli_locator.find_cli("tool") == (None, None)


def test_finds_a_cli_under_nvm_bin(tmp_path, monkeypatch):
    """$NVM_BIN survives into some non-interactive contexts even when PATH does
    not carry the bin dir."""
    nvm_bin = tmp_path / "nvm-bin"
    _make_exe(nvm_bin)
    monkeypatch.setenv("NVM_BIN", str(nvm_bin))
    found, where = cli_locator.find_cli("tool")
    assert Path(found).parent == nvm_bin
    assert where == str(nvm_bin)


def test_finds_a_cli_in_the_nvm_versions_tree(_clean_env):
    """The reported layout: ~/.nvm/versions/node/v24.19.0/bin/gemini."""
    bin_dir = _clean_env / ".nvm" / "versions" / "node" / "v24.19.0" / "bin"
    _make_exe(bin_dir)
    found, where = cli_locator.find_cli("tool")
    assert Path(found).parent == bin_dir
    assert where != "PATH"


def test_newest_node_version_wins(_clean_env):
    """Two Node versions, both with the CLI: the newer one is the one the user
    would get in their own shell."""
    root = _clean_env / ".nvm" / "versions" / "node"
    _make_exe(root / "v18.20.0" / "bin")
    _make_exe(root / "v24.19.0" / "bin")
    found, _where = cli_locator.find_cli("tool")
    assert "v24.19.0" in found


def test_node_version_ordering_is_numeric_not_lexicographic(_clean_env):
    """v9 sorts after v10 as a string; that is not what "newest" means."""
    root = _clean_env / ".nvm" / "versions" / "node"
    _make_exe(root / "v9.0.0" / "bin")
    _make_exe(root / "v10.0.0" / "bin")
    found, _where = cli_locator.find_cli("tool")
    assert "v10.0.0" in found


def test_finds_a_cli_under_volta(_clean_env):
    _make_exe(_clean_env / ".volta" / "bin")
    found, _where = cli_locator.find_cli("tool")
    assert ".volta" in found


def test_finds_a_cli_under_fnm(_clean_env):
    bin_dir = (_clean_env / ".local" / "share" / "fnm" / "node-versions"
               / "v22.1.0" / "installation" / "bin")
    _make_exe(bin_dir)
    found, _where = cli_locator.find_cli("tool")
    assert Path(found).parent == bin_dir


def test_search_locations_are_inspectable():
    """The "not found" message names how many places were checked, so the list
    has to be data rather than control flow buried in the probe."""
    locations = cli_locator.search_locations()
    assert len(locations) > 3
    assert all(isinstance(p, Path) for p in locations)


def test_a_missing_version_manager_root_is_not_an_error(_clean_env):
    """No nvm, no volta, no fnm — probing must stay silent, not raise."""
    assert cli_locator.find_cli("definitely-not-installed") == (None, None)
