"""VSO-01: the vault-only write path, present on every surface that decides one.

Incident 2026-07-13 ([[Error - Vault staging duplicates]]): a client edited 14
vault documents as copies under the user's profile root and copied them back,
because the sandbox exposed that directory as writable. No unique data was lost
— every staging file was byte-identical to, or contained in, its canonical
version — but the machine briefly had two working sets of the same notes, and
only a hash audit could say which was authoritative.

A rule saying "artifacts live in the vault" already existed. It was not present
where the write path gets chosen, which is the only place it could have helped
— so these tests are about PRESENCE, on all four client surfaces and in the
skill, from one source. They fail if any surface drops it (the acceptance
criterion for VSO-01).
"""
import importlib.util
from pathlib import Path

import pytest

from singularity.machine.hooks_state import CAPTURE_HOOKS_FRAGMENT, MEMORY_WRITES_FRAGMENT

ENGINE = Path(__file__).resolve().parents[1]
SKILL = ENGINE / "singularity" / "skills" / "mb-memory" / "SKILL.md"


def _load_registrar(name: str):
    spec = importlib.util.spec_from_file_location(name, ENGINE / "install" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- the fragment itself -----------------------------------------------------

def test_fragment_states_the_three_things_that_were_missing():
    f = MEMORY_WRITES_FRAGMENT.lower()
    assert "mb note new" in f, "the sanctioned route for a new note"
    assert "mirror" in f and "copy it back" in f, "no external staging/copy-back"
    assert "input" in f and "export" in f, "external folders are input/export only"
    assert "stop" in f, "blocked write must stop and ask, not route around"


def test_fragment_does_not_forbid_reading_or_importing():
    """The rule must not read as "never touch anything outside the vault" —
    input assets and an explicitly requested export stay legitimate."""
    assert "Reading vault files, and copying INTO the vault, are always fine." \
        in MEMORY_WRITES_FRAGMENT


def test_fragment_is_short_enough_to_inject_every_session():
    """It rides in every SessionStart payload; CAPTURE_HOOKS_FRAGMENT is the
    established size budget for a fragment of this kind."""
    assert len(MEMORY_WRITES_FRAGMENT) < 2 * len(CAPTURE_HOOKS_FRAGMENT)


# --- every surface that chooses a write path ---------------------------------

@pytest.mark.parametrize("registrar", ["register_codex", "register_gemini", "register_grok"])
def test_managed_profile_carries_the_policy(registrar, tmp_path):
    body = _load_registrar(registrar)._profile_body(tmp_path / "vault")
    assert MEMORY_WRITES_FRAGMENT in body, \
        f"{registrar} profile lost the vault write policy"


def test_claude_session_start_carries_the_policy(tmp_path, monkeypatch):
    from singularity.hooks import session_start

    vault = tmp_path / "vault"
    (vault / ".mb").mkdir(parents=True)
    (vault / ".mb" / "config.json").write_text("{}", encoding="utf-8")
    # build_context imports resolve_vault INSIDE the function, so the patch has
    # to land on singularity.config — patching the hook module's attribute is
    # shadowed by that import. It passed before only because resolve_vault then
    # read the developer's real ~/.singularity.json and found a real vault.
    import singularity.config as config_mod
    monkeypatch.setattr(config_mod, "resolve_vault", lambda explicit=None: vault)
    payload = session_start.build_context(vault)
    assert MEMORY_WRITES_FRAGMENT in payload


def test_skill_carries_the_destination_gate():
    body = SKILL.read_text(encoding="utf-8")
    assert "GATE перед любой записью: куда писать" in body
    assert "Error - Vault staging duplicates" in body, \
        "the gate should point at the incident it came from"


def test_all_surfaces_use_the_same_source():
    """Four copies of a policy drift; this is the failure mode the hook default
    already showed once (v0.6.3). Each surface must import the constant, not
    paraphrase it."""
    for name in ("register_codex", "register_gemini", "register_grok"):
        src = (ENGINE / "install" / f"{name}.py").read_text(encoding="utf-8")
        assert "MEMORY_WRITES_FRAGMENT" in src
        assert "hooks_state import" in src
    ss = (ENGINE / "singularity" / "hooks" / "session_start.py").read_text(encoding="utf-8")
    assert "MEMORY_WRITES_FRAGMENT" in ss


# --- the merge (2026-08-07): two fragments were one rule, and they disagreed ---

def test_only_one_fragment_governs_writing_to_the_vault():
    """`MEMORY_WRITES_FRAGMENT` and `MEMORY_WRITES_FRAGMENT` both told the
    reader how something gets into the vault, in the same SessionStart payload,
    and they gave different first instructions for a NEW note — contradiction K1
    of the 2026-08-06 audit. Two texts at one decision point is the shape the
    engine's own rule forbids; the fix is one text, not a tie-break."""
    from singularity.machine import hooks_state as hs
    assert not hasattr(hs, "VAULT_WRITE_POLICY_FRAGMENT"), \
        "the second vault fragment is still defined"
    assert hasattr(hs, "MEMORY_WRITES_FRAGMENT"), "the merged one must exist"


def test_a_new_note_has_exactly_one_stated_route():
    """K1 itself. `mb-memory/SKILL.md` says a new file in a managed root is
    `mb note new` ONLY, and `vault_guard` flags a hand-made one — while the old
    policy fragment opened with «Write **directly** to the vault path». Whatever
    the merged text says, it must not present a direct write as the route for a
    new note."""
    from singularity.machine.hooks_state import MEMORY_WRITES_FRAGMENT as f
    assert "mb note new" in f
    assert "**directly** to the vault path" not in f, \
        "the merged fragment still offers a direct write as the primary route"
    assert "EXISTING" in f, "the existing-note path must stay explicitly allowed"


def test_the_merged_fragment_keeps_what_the_staging_incident_bought():
    """The policy fragment exists because 14 vault documents were once edited as
    copies elsewhere and copied back. Merging must not drop that."""
    from singularity.machine.hooks_state import MEMORY_WRITES_FRAGMENT as f
    assert "mirror" in f and "copy it back" in f
    assert "second source of truth" in f
