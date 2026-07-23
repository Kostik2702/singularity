"""T15: skill_manifest.py is the single source of which client gets which
engine-owned skill. Before this, Claude's list (register_skills.SKILLS) and
the Codex/Gemini list (client_registration.PORTABLE_SKILLS) were two
independently-maintained literals with nothing forcing them to agree --
these tests lock in that every installer now derives its list from the
SAME manifest, that the manifest is a complete/accurate inventory of
singularity/skills/, and that every client-exclusion carries a reason.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]
SKILLS_SRC = ENGINE / "singularity" / "skills"
INSTALL = ENGINE / "install"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def skill_manifest():
    return _load("skill_manifest", INSTALL / "skill_manifest.py")


CLIENTS = ["claude", "codex", "gemini", "grok"]

# GRK-01: the approved portable baseline — every cross-client skill, in
# manifest order. Codex/Gemini golden regression AND the Grok launch set.
PORTABLE_BASELINE = [
    "mb-search", "mb-memory", "mb-llm-delegate", "mb-onboard", "mb-triage",
    "workflow-helper", "grill", "confidence-calibration", "memory-curator",
    "error-reports", "report", "compaction-plan",
]


# --- manifest <-> repo inventory agreement ------------------------------------

def test_every_manifest_skill_has_a_repo_directory(skill_manifest):
    missing = [name for name in skill_manifest.SKILL_MANIFEST if not (SKILLS_SRC / name).is_dir()]
    assert missing == []


def test_every_repo_skill_directory_is_in_the_manifest(skill_manifest):
    on_disk = {p.name for p in SKILLS_SRC.iterdir() if p.is_dir()}
    missing = on_disk - set(skill_manifest.SKILL_MANIFEST)
    assert missing == set(), f"skill(s) on disk but not declared in skill_manifest.py: {missing}"


# --- clients / reason invariants ----------------------------------------------

def test_every_skill_declares_at_least_one_client(skill_manifest):
    for name, meta in skill_manifest.SKILL_MANIFEST.items():
        assert meta["clients"], f"{name}: clients must not be empty"
        assert set(meta["clients"]) <= set(CLIENTS), f"{name}: unknown client in {meta['clients']}"


def test_partial_client_support_always_has_a_reason(skill_manifest):
    for name, meta in skill_manifest.SKILL_MANIFEST.items():
        if set(meta["clients"]) != set(CLIENTS):
            assert meta.get("reason"), f"{name}: excluded from some client(s) but no reason recorded"


def test_full_client_support_has_no_dangling_reason(skill_manifest):
    for name, meta in skill_manifest.SKILL_MANIFEST.items():
        if set(meta["clients"]) == set(CLIENTS):
            assert meta.get("reason") is None


# --- skills_for_client / exclusion_reason --------------------------------------

@pytest.mark.parametrize("client", CLIENTS)
def test_skills_for_client_matches_manifest_filter(skill_manifest, client):
    expected = [n for n, m in skill_manifest.SKILL_MANIFEST.items() if client in m["clients"]]
    assert skill_manifest.skills_for_client(client) == expected


@pytest.mark.parametrize("client", CLIENTS)
def test_skills_for_client_no_duplicates(skill_manifest, client):
    names = skill_manifest.skills_for_client(client)
    assert len(names) == len(set(names))


def test_exclusion_reason_none_when_supported():
    manifest = _load("skill_manifest", INSTALL / "skill_manifest.py")
    assert manifest.exclusion_reason("mb-search", "codex") is None


def test_exclusion_reason_present_when_not_supported():
    manifest = _load("skill_manifest", INSTALL / "skill_manifest.py")
    reason = manifest.exclusion_reason("council", "codex")
    assert reason and "Agent" in reason or "subagent" in (reason or "")


def test_exclusion_reason_none_for_unknown_skill():
    manifest = _load("skill_manifest", INSTALL / "skill_manifest.py")
    assert manifest.exclusion_reason("totally-unknown-skill", "codex") is None


def test_skills_for_unknown_client_is_empty(skill_manifest):
    assert skill_manifest.skills_for_client("cursor") == []
    assert skill_manifest.skills_for_client("") == []


# --- GRK-01: golden client matrix ----------------------------------------------

def test_codex_list_is_golden_baseline(skill_manifest):
    """Regression guard: adding grok must not change the Codex set."""
    assert skill_manifest.skills_for_client("codex") == PORTABLE_BASELINE


def test_gemini_list_is_golden_baseline(skill_manifest):
    """Regression guard: adding grok must not change the Gemini set."""
    assert skill_manifest.skills_for_client("gemini") == PORTABLE_BASELINE


def test_grok_list_equals_approved_portable_baseline(skill_manifest):
    """GRK-01: Grok launches with exactly the portable baseline — no council
    or visual/GT-gated skill is silently promoted (GRK-00 proved SKILL.md
    discovery + advertisement; Claude-only skills stay excluded with reasons)."""
    assert skill_manifest.skills_for_client("grok") == PORTABLE_BASELINE


def test_grok_exclusions_are_client_accurate(skill_manifest):
    """Every skill excluded from grok carries a reason, and the council family
    must not claim Grok lacks subagents (it has spawn_subagent — GRK-00)."""
    for name, meta in skill_manifest.SKILL_MANIFEST.items():
        if "grok" in meta["clients"]:
            continue
        reason = skill_manifest.exclusion_reason(name, "grok")
        assert reason, f"{name}: excluded from grok without a reason"
    for name in ("council", "planning-council", "observation-council"):
        reason = skill_manifest.exclusion_reason(name, "grok")
        assert "spawn_subagent" in reason, (
            f"{name}: reason must acknowledge Grok's subagent capability, "
            "not blanket-claim no executor exists"
        )


def test_council_family_claude_only(skill_manifest):
    for name in ("council", "planning-council", "observation-council"):
        assert skill_manifest.SKILL_MANIFEST[name]["clients"] == ["claude"]


# --- installers derive from the SAME manifest, not their own literal --------

def _load_with_sys_path(name: str):
    sys.path.insert(0, str(INSTALL))
    sys.path.insert(0, str(ENGINE))
    try:
        return _load(name, INSTALL / f"{name}.py")
    finally:
        sys.path.remove(str(INSTALL))
        sys.path.remove(str(ENGINE))


def test_register_skills_matches_manifest(skill_manifest):
    register_skills = _load_with_sys_path("register_skills")
    assert register_skills.SKILLS == skill_manifest.skills_for_client("claude")


def test_register_codex_matches_manifest(skill_manifest):
    register_codex = _load_with_sys_path("register_codex")
    assert register_codex.CODEX_SKILLS == skill_manifest.skills_for_client("codex")


def test_register_gemini_matches_manifest(skill_manifest):
    register_gemini = _load_with_sys_path("register_gemini")
    assert register_gemini.GEMINI_SKILLS == skill_manifest.skills_for_client("gemini")


def test_client_registration_no_longer_declares_portable_skills():
    # T15: PORTABLE_SKILLS was the pre-manifest divergence point (a second,
    # independently-maintained literal) -- it must be gone, not just unused.
    client_registration = _load_with_sys_path("client_registration")
    assert not hasattr(client_registration, "PORTABLE_SKILLS")
