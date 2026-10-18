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
    assert entry["tools"] == ["Agent"]
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


def test_team_lead_sends_councils_and_budget_back_to_the_owner():
    text = _text()
    assert "owner's explicit command" in text
    assert "AskUserQuestion" in text
    assert "run_in_background" in text
