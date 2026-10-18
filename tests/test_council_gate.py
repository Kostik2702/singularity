"""The three councils start only when the owner asks for one.

A council is four agents on the strongest model for several rounds: the most
expensive thing a session can start. `disable-model-invocation: true` keeps
the skill out of the model's list while `/council` still works for the owner,
and every spawn line names opus so a council never falls to the session's
default executor model.
"""
from pathlib import Path

import pytest
import yaml

ENGINE = Path(__file__).resolve().parents[1]
SKILLS = ENGINE / "singularity" / "skills"
COUNCILS = ["council", "planning-council", "observation-council"]


def _text(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


def _frontmatter(name: str) -> list[str]:
    lines = _text(name).splitlines()
    assert lines[0] == "---", f"{name}: no frontmatter"
    return lines[1:lines.index("---", 1)]


@pytest.mark.parametrize("name", COUNCILS)
def test_councils_hidden_from_the_model(name):
    assert "disable-model-invocation: true" in _frontmatter(name)


@pytest.mark.parametrize("name", COUNCILS)
def test_council_frontmatter_is_read_by_the_harness(name):
    """The flag only acts if the harness can parse the block. An unquoted
    description holding `word: ` is not YAML: the harness then drops the whole
    frontmatter, shows the first body line as the description, and the skill
    stays visible to the model with the flag sitting there unread."""
    meta = yaml.safe_load("\n".join(_frontmatter(name)))
    assert meta["disable-model-invocation"] is True
    assert meta["name"] == name


@pytest.mark.parametrize("name", COUNCILS)
def test_council_agents_run_on_opus(name):
    spawn_lines = [line for line in _text(name).splitlines() if "Agent" in line and 'model: "opus"' in line]
    assert spawn_lines, f"{name}: no spawn instruction names model: \"opus\""


@pytest.mark.parametrize("name", COUNCILS)
def test_council_says_who_starts_it(name):
    body = _text(name).split("---", 2)[2]
    assert "owner's explicit command" in body
