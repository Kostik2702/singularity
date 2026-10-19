"""Every agent the engine ships carries the model tier it runs on by default.

A spawn that names no model takes the agent's `model:` line; without one it
falls to the session default, and before that default existed every helper
quietly ran on the lead's own model. The tier lives in the frontmatter so the
choice is made once, where the agent is defined, and this file fails when a
line goes missing.
"""
import importlib.util
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
AGENTS = ENGINE / "singularity" / "agents"

DEFAULT_MODELS = {
    "scout": "haiku",
    "advisor": "fable",
    "coder": "sonnet",
    "web-implementer": "sonnet",
    "historian": "sonnet",
    "investigator": "opus",
    "critic": "opus",
    "validator": "opus",
    "reviewer": "opus",
    "web-viewer": "opus",
}


def frontmatter(name: str) -> list[str]:
    """The lines between the leading `---` fences of an agent file."""
    lines = (AGENTS / f"{name}.md").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---", f"{name}.md does not open with frontmatter"
    end = lines.index("---", 1)
    return lines[1:end]


def test_agent_default_models():
    for name, model in DEFAULT_MODELS.items():
        assert f"model: {model}" in frontmatter(name), f"{name} should default to {model}"


def test_scout_skips_claude_md():
    assert "omitClaudeMd: true" in frontmatter("scout")
    assert not any(line.startswith("omitClaudeMd") for line in frontmatter("coder"))


def test_new_agents_are_deployed():
    spec = importlib.util.spec_from_file_location("register_agents", ENGINE / "install" / "register_agents.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert "scout" in module.AGENTS
    assert "advisor" in module.AGENTS


def test_preamble_returns_questions_without_stopping():
    preamble = (AGENTS / "AGENT_SHARED_PREAMBLE.md").read_text(encoding="utf-8")
    section = preamble.split("## §2.", 1)[1].split("\n## ", 1)[0]
    assert "never stop the run to wait" in section


def test_preamble_no_stop_rule_is_a_registered_delivery():
    """The rule acts in every subagent, so the registry names it: deleting the
    line must turn the delivery check red, not only this file."""
    from singularity.machine import rules_delivery

    rows = [r for r in rules_delivery.DELIVERIES if r[3] == "never stop the run to wait"]
    assert len(rows) == 1 and rows[0][1] == "preamble §2"


def test_advisor_is_not_asked_for_what_its_tools_cannot_do():
    """The preamble's §1 asks for `mb search` and §5 for the confidence skill;
    the advisor has neither Bash nor Skill, so its own file says how it meets them."""
    lines = frontmatter("advisor")
    tools = next(line for line in lines if line.startswith("tools:"))
    assert "Bash" not in tools and "Skill" not in tools, "control: advisor tools changed, revisit this test"
    body = (AGENTS / "advisor.md").read_text(encoding="utf-8")
    assert "the lead brings the vault facts" in body
    assert "additive model by hand" in body
