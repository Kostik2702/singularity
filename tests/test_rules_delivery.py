"""Rules act where the action is chosen — these tests are about PRESENCE.

The failure they guard against is not a bug in a rule, it is a rule that is
somewhere else. `rules/` in the vault held 72 of them; 37 had no delivery point
at all, and the ones that did had it by hand — nothing failed when an injection
was dropped. That is the same shape as the six defects of 2026-08-04, where the
knowledge existed and was absent at the moment of action.

Second thing under test here: the rules a subagent starts on ship with the
ENGINE. They used to be read from `{{VAULT}}/rules/AGENT_SHARED_PREAMBLE.md`,
which quietly made "what the machine does" a property of "which vault is
configured" — swap the vault and eight agents start with no rules, with nothing
printed. Knowledge lives in the vault; mechanism does not.
"""
import importlib.util
from pathlib import Path

import pytest

from singularity.machine import rules_delivery
from singularity.machine.hooks_state import (
    CAPTURE_HOOKS_FRAGMENT,
    MEMORY_WRITES_FRAGMENT,
    RULE_POINT_FRAGMENT,
)

ENGINE = Path(__file__).resolve().parents[1]
AGENT_NAMES = ["investigator", "critic", "validator", "historian",
               "reviewer", "coder", "web-implementer", "web-viewer"]


# --- the registry ------------------------------------------------------------

def test_every_registered_delivery_is_still_in_place():
    """The whole point of the registry: an injection that gets refactored away
    fails here instead of going unnoticed until the rule is needed."""
    missing = rules_delivery.check()
    assert missing == [], "delivery lost:\n  " + "\n  ".join(missing)


def test_a_rule_without_a_point_is_recorded_rather_than_dropped():
    """When a delivery point disappears, the entry must not just vanish from
    DELIVERIES — a silent removal is indistinguishable from a rule that was
    never delivered. v0.12.0 deleted the reel tool with its skill, which is how
    this list got its first member."""
    for rule, why in rules_delivery.WITHOUT_A_POINT:
        assert why.strip(), f"{rule}: an orphaned rule needs a stated reason"
        assert not any(r == rule for r, _, _, _ in rules_delivery.DELIVERIES), \
            f"{rule} is listed both as delivered and as pointless"


def test_the_inventory_adds_up():
    """72 rule notes were inventoried: what DELIVERIES covers, plus the one the
    release orphaned, plus what belongs to another repo / platform / to the code
    itself. The point is not the arithmetic — it is that an untouched rule
    cannot hide behind "probably handled somewhere"."""
    accounted = {r for r, _, _, _ in rules_delivery.DELIVERIES}
    assert len(accounted) >= 20, "the delivered set shrank unexpectedly"
    assert rules_delivery.NOT_OURS_TO_DELIVER, \
        "the out-of-scope groups must stay written down, not remembered"
    for group, why in rules_delivery.NOT_OURS_TO_DELIVER:
        assert why.strip(), f"{group}: an out-of-scope group needs a stated reason"


def test_the_registry_covers_every_surface_kind():
    """A registry that only ever grew skill entries would silently stop guarding
    the two surfaces that carry the universal rules."""
    surfaces = {surface for _, surface, _, _ in rules_delivery.DELIVERIES}
    assert "SessionStart" in surfaces           # main thread
    assert any(s.startswith("preamble") for s in surfaces)   # every subagent
    assert "skill" in surfaces


def test_registered_paths_all_live_in_the_engine():
    """A delivery point inside the vault would reintroduce the dependency this
    whole change removes: no vault, no rules."""
    for rule, surface, path, _ in rules_delivery.DELIVERIES:
        assert ENGINE in path.parents, \
            f"{rule} -> {surface}: {path} is outside the engine"


# --- the preamble ships with the engine, not with the vault ------------------

def test_preamble_exists_in_the_engine():
    assert rules_delivery.PREAMBLE.is_file(), \
        "the shared preamble must be an engine file, not a vault note"


def test_no_agent_reads_its_rules_out_of_the_vault():
    offenders = rules_delivery.vault_dependencies()
    assert offenders == [], \
        f"agents still addressing the preamble through the vault: {offenders}"


@pytest.mark.parametrize("name", AGENT_NAMES)
def test_every_agent_points_at_the_deployed_preamble(name):
    src = (ENGINE / "singularity" / "agents" / f"{name}.md").read_text(encoding="utf-8")
    assert "{{AGENTS}}/AGENT_SHARED_PREAMBLE.md" in src, \
        f"{name} does not name the engine-deployed preamble"


def test_preamble_keeps_its_mandatory_minimum_addressable():
    """Agents are told to read §1+§2 before acting and §4+§5 before returning.
    If a section is renamed, that instruction points at nothing."""
    body = rules_delivery.PREAMBLE.read_text(encoding="utf-8")
    for section in ("## §1.", "## §2.", "## §4.", "## §5."):
        assert section in body, f"{section} missing — agent instructions address it by number"


def test_installer_deploys_the_preamble_with_the_agents():
    spec = importlib.util.spec_from_file_location(
        "register_agents_probe", ENGINE / "install" / "register_agents.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.PREAMBLE == "AGENT_SHARED_PREAMBLE"
    assert (mod.AGENTS_SRC / f"{mod.PREAMBLE}.md").is_file(), \
        "the installer would deploy a preamble that is not in the repo"


def test_agents_dir_token_resolves_without_a_vault():
    """`{{AGENTS}}` must not be answered through vault resolution — that is the
    dependency being removed. A machine with no vault still gets its rules."""
    spec = importlib.util.spec_from_file_location(
        "vault_token_probe", ENGINE / "install" / "vault_token.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rendered = mod.render_text("read {{AGENTS}}/AGENT_SHARED_PREAMBLE.md",
                               Path("/nonexistent-vault"), Path("/deployed/agents"))
    assert rendered == "read /deployed/agents/AGENT_SHARED_PREAMBLE.md"
    assert "{{AGENTS}}" not in rendered


# --- the two new SessionStart fragments --------------------------------------

def test_memory_fragment_states_the_substitution_that_kept_happening():
    f = MEMORY_WRITES_FRAGMENT
    assert "THE VAULT" in f, "«память» = vault, not the harness-local store"
    assert "mb note new" in f and "mb learn" in f, "writes go through the CLI"
    assert "mb search" in f, "search before hand-walking the vault"
    assert "never make the machine's behaviour" in f, \
        "the fragment must also say that mechanism does not live in the vault"


def test_rule_point_fragment_demands_a_point_a_source_and_a_test():
    f = RULE_POINT_FRAGMENT
    assert "Name the decision point" in f
    assert "ONE source" in f and "test that fails" in f, \
        "several points need one source plus a presence test"
    assert "counter-text" in f, "the text already standing at that point wins"
    assert "RECORDED decision" in f, \
        "do not 'fix' a deliberate decision you have not looked for"


@pytest.mark.parametrize("fragment", [MEMORY_WRITES_FRAGMENT, RULE_POINT_FRAGMENT])
def test_new_fragments_stay_within_the_session_budget(fragment):
    """Every fragment is paid for in every session; CAPTURE_HOOKS_FRAGMENT is
    the established size unit for one."""
    assert len(fragment) < 2 * len(CAPTURE_HOOKS_FRAGMENT)


def test_both_fragments_reach_every_session(tmp_path, monkeypatch):
    """Same wiring check as the vault-write policy: the constant existing is not
    the same as the hook emitting it."""
    from singularity.hooks import session_start

    vault = tmp_path / "vault"
    (vault / ".mb").mkdir(parents=True)
    (vault / ".mb" / "config.json").write_text("{}", encoding="utf-8")
    import singularity.config as config_mod
    monkeypatch.setattr(config_mod, "resolve_vault", lambda explicit=None: vault)

    payload = session_start.build_context(str(vault))
    assert MEMORY_WRITES_FRAGMENT in payload
    assert RULE_POINT_FRAGMENT in payload


def test_session_start_imports_the_fragments_from_one_source():
    """Paraphrasing a fragment into the hook is how two copies start drifting."""
    ss = (ENGINE / "singularity" / "hooks" / "session_start.py").read_text(encoding="utf-8")
    assert "MEMORY_WRITES_FRAGMENT" in ss and "RULE_POINT_FRAGMENT" in ss
    assert "hooks_state import" in ss
