"""GCLI-06: opt-in end-to-end test against a REAL @google/gemini-cli.

Auto-skips when no `gemini` is on PATH, so it is inert on the normal 3-OS matrix
and only runs in the dedicated E2E job (which installs Node + the CLI). Exercises
the full install -> doctor -> uninstall lifecycle with the real binary providing
the version, in a fully isolated HOME.
"""
import importlib.util
import json
import pathlib
import shutil

import pytest

pytestmark = [
    pytest.mark.gemini_e2e,
    pytest.mark.skipif(shutil.which("gemini") is None,
                       reason="real @google/gemini-cli not on PATH"),
]

ENGINE = pathlib.Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ENGINE / "install" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_real_client_full_lifecycle(tmp_path, monkeypatch):
    rg = _load("register_gemini")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    vault = tmp_path / "vault"
    (vault / ".mb").mkdir(parents=True)
    monkeypatch.setattr(rg, "resolve_vault", lambda *a, **k: vault)

    # --- install ---
    assert rg.main() == 0
    gm = home / ".gemini" / "GEMINI.md"
    assert gm.exists() and rg.BEGIN in gm.read_text(encoding="utf-8")
    for name in rg.GEMINI_SKILLS:
        assert (home / ".gemini" / "skills" / name).exists(), name
    # the REAL binary supplied the version, so both hooks are registered
    settings = json.loads((home / ".gemini" / "settings.json").read_text(encoding="utf-8"))
    ss = [h["name"] for d in settings["hooks"]["SessionStart"] for h in d["hooks"]]
    aa = [h["name"] for d in settings["hooks"]["AfterAgent"] for h in d["hooks"]]
    assert rg.SESSION_START_HOOK_NAME in ss
    assert rg.AFTER_AGENT_HOOK_NAME in aa

    # --- doctor sees a working path (real version -> E1 PASS, all E* PASS) ---
    from singularity.doctor import Report, gemini_block
    rep = Report(quiet=True)
    gemini_block(rep, fix=False)
    statuses = {c["id"]: c["status"] for c in rep.checks}
    assert statuses.get("E1-gemini") == "PASS", statuses
    assert statuses.get("E2-gemini-profile") == "PASS"
    assert statuses.get("E3-gemini-skills") == "PASS"
    assert statuses.get("E4-gemini-hooks") == "PASS"

    # --- uninstall round-trip: only owned artifacts removed ---
    res = rg.uninstall()
    assert res["profile"] == "removed"
    assert res["hooks"] == 2
    assert res["skills_removed"] == len(rg.GEMINI_SKILLS)
    # GEMINI.md held only our block, so it is removed entirely; if any user text
    # had been present the file would remain without our markers.
    assert not gm.exists() or rg.BEGIN not in gm.read_text(encoding="utf-8")
    assert not (home / ".gemini" / "skills" / rg.GEMINI_SKILLS[0]).exists()
