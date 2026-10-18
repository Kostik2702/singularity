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
    THINK_FIRST_FRAGMENT,
    VERIFICATION_FRAGMENT,
)

ENGINE = Path(__file__).resolve().parents[1]
AGENT_NAMES = ["investigator", "critic", "validator", "historian",
               "reviewer", "coder", "web-implementer", "web-viewer",
               "scout", "advisor"]


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

    # The half that was missing. `>= 20` and "the list is non-empty" are floors:
    # they hold while a rule sits in no bucket at all, which is what happened.
    # The arithmetic cannot be done here — it needs the vault, and the engine may
    # not depend on one — so what this asserts is that the MECHANISM which can do
    # it exists and is wired, and `tests/test_doctor.py` covers its wiring.
    assert callable(rules_delivery.unaccounted_rules), \
        "nothing can reconcile the inventory against the vault"
    assert rules_delivery.DELIVERY_SECTION_MARKERS, \
        "a note declares its delivery point by a section; the heading must be named"
    for r, _, _, _ in rules_delivery.DELIVERIES:
        assert not any(r == g for g, _ in rules_delivery.NOT_OURS_TO_DELIVER), \
            f"{r} is both delivered and declared out of scope"


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
    assert "THE VAULT" in f, "the user's word for memory = vault, not the harness-local store"
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


#: EVERY fragment, not the two this test was born with. Review found the
#: assertion had been hand-copied into three test files over disjoint subsets,
#: and that VERIFICATION_FRAGMENT was covered by none of them — the pattern
#: leaks by construction, so the list lives in one place.
ALL_FRAGMENTS = {
    "CAPTURE_HOOKS": CAPTURE_HOOKS_FRAGMENT,
    "MEMORY_WRITES": MEMORY_WRITES_FRAGMENT,
    "RULE_POINT": RULE_POINT_FRAGMENT,
    "VERIFICATION": VERIFICATION_FRAGMENT,
    "THINK_FIRST": THINK_FIRST_FRAGMENT,
}


@pytest.mark.parametrize("name", sorted(ALL_FRAGMENTS))
def test_every_fragment_stays_within_the_session_budget(name):
    """Every fragment is paid for in every session; CAPTURE_HOOKS_FRAGMENT is
    the established size unit for one."""
    assert len(ALL_FRAGMENTS[name]) < 2 * len(CAPTURE_HOOKS_FRAGMENT)


def test_the_fragments_have_an_aggregate_ceiling_too():
    """Six fragments each under the per-item limit would pass at 7368 chars,
    while the cost that matters is the sum — paid every session. The ceiling is
    the number `docs/rules-delivery.md` documents, so the doc cannot drift from
    reality without this failing."""
    total = sum(len(f) for f in ALL_FRAGMENTS.values())
    assert total <= 4150, (
        f"fragments total {total} chars; raise the ceiling here AND the figure "
        f"in docs/rules-delivery.md in the same change, or cut text")


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


def test_capture_fragment_names_the_three_commands_and_its_own_boundary():
    """The fragment had a length budget and a verbatim-copy check, and nothing
    about what it SAYS. It was reworded on 2026-08-08 (the trigger phrases came
    out) with no test able to notice if a clause had gone with them — the same
    hole `MEMORY_WRITES` and `RULE_POINT` were already covered against."""
    f = CAPTURE_HOOKS_FRAGMENT
    for command in ("mb hooks status", "mb hooks enable", "mb hooks disable"):
        assert command in f, f"{command} is the deterministic path and must be named"
    assert "never SessionStart" in f and "guard hook" in f, \
        "the fragment's whole job is bounding WHICH hooks are meant"
    assert "confirm scope" in f, \
        "a vague 'turn off the hooks' must not default to the capture hooks"
    assert "settings.json" in f, "hand-editing the file has to stay forbidden"


def test_the_stop_reminder_still_asks_for_what_it_is_for():
    """`stop_capture.REASON` is injected at the end of every session and had
    ZERO test references — it was rewritten with nothing covering a word of it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "stop_capture_probe", ENGINE / "singularity" / "hooks" / "stop_capture.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    r = mod.REASON
    assert "mb learn" in r and "--task" in r, "the sanctioned write path must be named"
    assert "file:line" in r, "code goes in as a reference, not as a paste"
    assert "more than one attempt" in r, "the criterion is what keeps this from being noise"
    assert "write nothing" in r, \
        "'nothing to record' must stay a legal outcome, or the reminder manufactures entries"


# --- the inventory, made countable -------------------------------------------
#
# `test_the_inventory_adds_up` used to assert `len(accounted) >= 20` and that the
# out-of-scope list was non-empty. Neither reads the vault, so neither could
# notice a rule note sitting in none of the three buckets — which is exactly how
# `Rule - a release against a class of defect gives a scanner for the class`
# stayed undelivered while the registry's docstring described an inventory that
# "adds up". These exercise the classifier against a FIXTURE directory, so they
# run on a machine with no vault and cannot skip themselves.

def _rules_fixture(tmp_path):
    d = tmp_path / "rules"
    d.mkdir()
    (d / "Rule - grafted.md").write_text(
        "# x\n\n## Where it is grafted\n\npreamble §7\n", encoding="utf-8")
    (d / "Rule - grafted in the other language.md").write_text(
        "# x\n\n## Где вживлено\n\n"
        "preamble\n", encoding="utf-8")
    (d / "Rule - named by the engine.md").write_text("# x\n\nno section\n", encoding="utf-8")
    (d / "Rule - nobody accounted for this.md").write_text("# x\n\nnothing\n", encoding="utf-8")
    return d


def test_a_note_that_declares_its_delivery_point_is_accounted(tmp_path):
    d = _rules_fixture(tmp_path)
    orphans = rules_delivery.unaccounted_rules(d, engine_text="")
    assert "Rule - grafted" not in orphans
    assert "Rule - grafted in the other language" not in orphans, \
        "the section heading exists in both languages in the vault"


def test_a_note_the_engine_names_is_accounted(tmp_path):
    """The Core Disciplines carry no section: they reach every agent as
    `[[wikilinks]]` from the preamble, which is a delivery point too."""
    d = _rules_fixture(tmp_path)
    engine = "see [[Rule - named by the engine]] for the detail"
    assert "Rule - named by the engine" not in rules_delivery.unaccounted_rules(d, engine)


def test_a_note_with_neither_is_reported(tmp_path):
    """The case the whole check exists for, and the control for the two above:
    without it, a classifier that accounted for everything would satisfy them."""
    d = _rules_fixture(tmp_path)
    orphans = rules_delivery.unaccounted_rules(d, engine_text="")
    assert "Rule - nobody accounted for this" in orphans
    assert "Rule - named by the engine" in orphans, \
        "with an empty engine text this one has no delivery point either"


def test_the_classifier_survives_a_missing_directory(tmp_path):
    """A machine with no vault must get an empty answer, not an exception: the
    doctor turns this into SKIP, and a crash there would read as a FAIL."""
    assert rules_delivery.unaccounted_rules(tmp_path / "nope", engine_text="") == []


def test_the_inventory_ratchet_is_recorded_and_reachable():
    """The ceiling is a number someone measured, not a placeholder: it has to be
    smaller than the vault's rule count for the ratchet to mean anything."""
    assert isinstance(rules_delivery.UNACCOUNTED_CEILING, int)
    assert rules_delivery.UNACCOUNTED_CEILING >= 0
