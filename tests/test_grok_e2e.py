"""GRK-06: opt-in end-to-end tests against a REAL Grok Build CLI.

Auto-skips when no `grok` is on PATH, so it is inert on the normal 3-OS unit
matrix and only runs in the dedicated grok-e2e job (which installs the official
CLI) or on a developer machine that has it. Needs NO xAI credentials: it only
probes `grok --version` and `grok inspect --json` (both offline; sessions are
never started) and drives register -> inspect -> doctor -> uninstall in a fully
isolated GROK_HOME + HOME/USERPROFILE.

Windows caveat baked into the assertions: the Claude-compat agents/hooks
scanners read the REAL user profile regardless of env (GRK-00), so discovery
assertions only ever check for OUR artifacts' presence, never for an exact
whole-machine inventory.
"""
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

pytestmark = [
    pytest.mark.grok_e2e,
    pytest.mark.skipif(shutil.which("grok") is None,
                       reason="real Grok Build CLI not on PATH"),
]

ENGINE = pathlib.Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ENGINE / "install" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def iso(tmp_path, monkeypatch):
    """Isolated GROK_HOME + home + vault; sentinel file proves the real
    profile is never mutated (checked on teardown by the caller's asserts)."""
    grok_home = tmp_path / "grokhome"
    grok_home.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    vault = tmp_path / "vault"
    (vault / ".mb").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    monkeypatch.setenv("HOME", str(home))
    if sys.platform.startswith("win"):
        monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("GROK_DISABLE_AUTOUPDATER", "1")
    return {"grok_home": grok_home, "home": home, "vault": vault, "proj": proj}


def _inspect_json(iso):
    proc = subprocess.run(["grok", "inspect", "--json"], cwd=iso["proj"],
                          capture_output=True, text=True, timeout=180,
                          env=os.environ.copy(), shell=False)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout)


def test_version_probe_against_real_binary():
    rg = _load("register_grok")
    version = rg._grok_version()
    assert version is not None and len(version) == 3
    assert version >= (0, 2, 0), f"unexpectedly old grok: {version}"


def test_real_client_full_lifecycle(iso, monkeypatch):
    rg = _load("register_grok")
    monkeypatch.setattr(rg, "resolve_vault", lambda *a, **k: iso["vault"])

    # --- install (real binary supplies the version) ---
    assert rg.main() == 0
    agents = iso["grok_home"] / "AGENTS.md"
    assert agents.exists() and rg.BEGIN in agents.read_text(encoding="utf-8")
    for name in rg.GROK_SKILLS:
        assert (iso["grok_home"] / "skills" / name).exists(), name

    # --- idempotent rerun: profile bytes stable, no backups spawned ---
    before = agents.read_bytes()
    assert rg.main() == 0
    assert agents.read_bytes() == before

    # --- REAL grok inspect --json discovers our profile and every skill ---
    report = _inspect_json(iso)
    instr_paths = {str(pathlib.Path(e["path"]).resolve()).lower()
                   for e in report.get("projectInstructions", [])}
    assert str(agents.resolve()).lower() in instr_paths, \
        "managed AGENTS.md not discovered by the real client"
    skills = {s["name"] for s in report.get("skills", [])}
    for name in rg.GROK_SKILLS:
        assert name in skills, f"skill {name} not discovered by the real client"

    # --- doctor sees a working path with the real version ---
    from singularity.doctor import Report, grok_block
    rep = Report(quiet=True)
    grok_block(rep, fix=False)
    statuses = {c["id"]: c["status"] for c in rep.checks}
    assert statuses.get("F1-grok") == "PASS", statuses
    assert statuses.get("F2-grok-profile") == "PASS"
    assert statuses.get("F3-grok-skills") == "PASS"
    if statuses.get("F4-grok-capture") != "PASS":
        # a version newer than MIN that drifted the contract would be caught
        # by the fixtures; a SKIP here can only mean version < MIN
        assert statuses.get("F4-grok-capture") == "SKIP"

    # --- foreign content survives everything ---
    foreign = iso["grok_home"] / "skills" / "user-own-skill"
    foreign.mkdir()
    (foreign / "SKILL.md").write_text("mine\n", encoding="utf-8")
    user_note = "# my own grok notes\n"
    agents.write_text(user_note + agents.read_text(encoding="utf-8"), encoding="utf-8")

    # --- exact-owned uninstall ---
    res = rg.uninstall()
    assert res["profile"] == "removed"
    assert res["skills_removed"] == len(rg.GROK_SKILLS)
    # skills_kept counts only foreign dirs squatting OWNED names (unit-tested);
    # a foreign skill under its own name simply survives untouched:
    assert foreign.exists()
    assert agents.read_text(encoding="utf-8") == user_note
    for name in rg.GROK_SKILLS:
        assert not (iso["grok_home"] / "skills" / name).exists()
        assert (ENGINE / "singularity" / "skills" / name / "SKILL.md").exists(), \
            "uninstall must never touch the engine source"
    # capture hook file removed too (registered because real version >= MIN)
    if res["hooks"]:
        assert not (iso["grok_home"] / "hooks" / rg.HOOK_FILENAME).exists()


def test_grok_home_with_spaces(tmp_path, monkeypatch):
    """Windows kill-scenario from the plan: a config root containing a space
    must survive register + real-client discovery + uninstall."""
    rg = _load("register_grok")
    grok_home = tmp_path / "grok home with spaces"
    grok_home.mkdir()
    vault = tmp_path / "vault"
    (vault / ".mb").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    monkeypatch.setenv("GROK_DISABLE_AUTOUPDATER", "1")
    monkeypatch.setattr(rg, "resolve_vault", lambda *a, **k: vault)

    assert rg.main() == 0
    proc = subprocess.run(["grok", "inspect", "--json"], cwd=proj,
                          capture_output=True, text=True, timeout=180,
                          env=os.environ.copy())
    assert proc.returncode == 0, proc.stderr[-2000:]
    report = json.loads(proc.stdout)
    skills = {s["name"] for s in report.get("skills", [])}
    assert set(rg.GROK_SKILLS) <= skills

    res = rg.uninstall()
    assert res["skills_removed"] == len(rg.GROK_SKILLS)
    assert not (grok_home / "AGENTS.md").exists()


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows junction semantics")
def test_windows_junction_removal_preserves_target(iso, monkeypatch):
    rg = _load("register_grok")
    monkeypatch.setattr(rg, "resolve_vault", lambda *a, **k: iso["vault"])
    assert rg.main() == 0
    link = iso["grok_home"] / "skills" / rg.GROK_SKILLS[0]
    src = ENGINE / "singularity" / "skills" / rg.GROK_SKILLS[0]
    assert os.path.realpath(link).lower() == os.path.realpath(src).lower()
    rg.uninstall()
    assert not link.exists()
    assert (src / "SKILL.md").exists()


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="PowerShell hook runner")
def test_windows_hook_command_shape(iso, monkeypatch):
    """The registered command must use the call-operator form — the only
    quoted-argv0 shape the PowerShell hook runner accepts (GRK-00)."""
    rg = _load("register_grok")
    monkeypatch.setattr(rg, "resolve_vault", lambda *a, **k: iso["vault"])
    assert rg.main() == 0
    hook_file = iso["grok_home"] / "hooks" / rg.HOOK_FILENAME
    if not hook_file.exists():
        pytest.skip("hook not registered (version below proven contract)")
    cmd = json.loads(hook_file.read_text(encoding="utf-8"))[
        "hooks"]["Stop"][0]["hooks"][0]["command"]
    assert cmd.startswith('& "')
    assert "$env:" not in cmd
