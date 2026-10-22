"""The lead's delegation rubric: who does the work, on which model, at which effort.

The Agent tool lets the lead set `effort` only when an instruction explicitly
asks for it, so the grant itself is load-bearing: without that sentence the
lead never sets effort and every executor runs at the session's level.
"""
import importlib.util
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
SKILL = ENGINE / "singularity" / "skills" / "team-lead" / "SKILL.md"


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _manifest():
    spec = importlib.util.spec_from_file_location("skill_manifest", ENGINE / "install" / "skill_manifest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_team_lead_registered_for_claude_only():
    entry = _manifest().SKILL_MANIFEST["team-lead"]
    assert entry["clients"] == ["claude"]
    assert entry["tools"] == ["Agent", "Bash"], "the skill drives `mb board` through Bash"
    assert entry["reason"]


def test_team_lead_grants_effort():
    text = _text()
    assert "`effort`" in text
    assert "explicit instruction" in text


def test_team_lead_has_the_tier_table():
    rows = [line for line in _text().splitlines() if line.startswith("|")]
    for tier in ("haiku", "sonnet", "opus", "fable"):
        assert any(tier in row for row in rows), f"no tier-table row for {tier}"


def test_team_lead_has_scale_and_stop_rules():
    text = _text()
    for phrase in ("2–4", "stop criterion", "path and a summary"):
        assert phrase in text, phrase


def test_a_missing_model_is_not_promised_to_be_sonnet():
    """A spawn with no model takes the agent's own `model:` line first; only
    then the session default, and with none set, the lead's own model. Saying
    "no model means sonnet" is true only on a machine with that default set,
    and seven engine agents default to something else."""
    from singularity.machine.hooks_state import TEAM_LEAD_FRAGMENT

    preamble = (ENGINE / "singularity" / "agents" / "AGENT_SHARED_PREAMBLE.md").read_text(encoding="utf-8")
    for text in (_text(), TEAM_LEAD_FRAGMENT, preamble):
        assert "means sonnet" not in text
        assert "the default when no model is named" not in text
    assert "own `model:` line" in _text()
    assert "agent's default" in TEAM_LEAD_FRAGMENT


def test_delegation_delivery_markers_name_one_place_each():
    """A marker that occurs twice keeps the registry green after the rule it
    guards is deleted — the other occurrence still matches. Checked on the rows
    this change registered, read from the registry itself."""
    from singularity.machine import rules_delivery

    files = {"scout.md", "advisor.md", "SKILL.md"}
    rows = [r for r in rules_delivery.DELIVERIES
            if r[2].name in files and r[2].parent.name in ("agents", "team-lead")]
    assert len(rows) >= 6, "control: the delegation rows were not found"
    for name, _surface, path, marker in rows:
        count = path.read_text(encoding="utf-8").count(marker)
        assert count == 1, f"{name}: marker {marker!r} occurs {count} times in {path.name}"


def test_the_measured_axis_rows_stay_together():
    """The two SessionStart rows of one rule sit next to each other under their
    incident comment; delegation rows went in between them once."""
    from singularity.machine import rules_delivery

    names = [r[0] for r in rules_delivery.DELIVERIES if r[1] == "SessionStart"]
    axis = [i for i, n in enumerate(names) if n == "measure the axis you were asked about"]
    assert len(axis) == 2 and axis[1] == axis[0] + 1


def test_team_lead_sends_councils_and_budget_back_to_the_owner():
    text = _text()
    assert "owner's explicit command" in text
    assert "AskUserQuestion" in text
    assert "run_in_background" in text


def test_team_lead_works_through_tickets_and_does_not_do_the_work():
    """The lead is the PM and the tech lead: any work is a ticket, and the lead
    does not do it, not even a one-line edit. The old rubric said "do it yourself
    when the brief costs as much as the work"; a rule of the opposite sign in the
    same text would win by being last, so it must be gone."""
    text = _text()
    assert "Any work is a ticket on the board" in text
    assert "mb note new ticket" in text
    assert "INDEPENDENT check" in text
    low = text.lower()
    for old in ("do it yourself", "do it yourself when", "brief would cost as much"):
        assert old not in low, old
    assert "not even a one-line edit" in text
    assert "permission" in low and "ask the owner" in low


def test_team_lead_fragment_carries_the_ticket_rule():
    from singularity.machine.hooks_state import TEAM_LEAD_FRAGMENT

    assert "Work runs on tickets" in TEAM_LEAD_FRAGMENT
    assert "agent ID" in TEAM_LEAD_FRAGMENT
    assert "never do the work yourself" in TEAM_LEAD_FRAGMENT


def test_team_lead_covers_sections_one_to_twelve():
    import re

    heads = re.findall(r"^## (\d+)\. ", _text(), flags=re.M)
    assert heads == [str(n) for n in range(1, 13)]


def test_every_board_subcommand_named_by_the_lead_surfaces_exists_in_the_parser():
    """The skill and the SessionStart fragment name `mb board <sub>`; a sub that
    the parser does not know turns the rule into an error at the point of use."""
    import re

    from singularity.cli import _build_parser
    from singularity.machine.hooks_state import TEAM_LEAD_FRAGMENT

    top = _build_parser()._subparsers._group_actions[0].choices
    known = set(top["board"]._subparsers._group_actions[0].choices)
    assert {"next", "show", "move", "note", "assign", "check"} <= known, "control"
    named = set()
    preamble = (ENGINE / "singularity" / "agents" / "AGENT_SHARED_PREAMBLE.md").read_text(encoding="utf-8")
    for text in (_text(), TEAM_LEAD_FRAGMENT, preamble):
        named |= set(re.findall(r"mb board ([a-z][a-z-]*)", text))
    assert {"assign", "move", "note", "next", "show", "check"} <= named, "control: the surfaces name the board"
    assert named <= known, f"unknown board subcommands: {sorted(named - known)}"
