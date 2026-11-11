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

DEFAULT_EFFORTS = {
    "scout": "low",
    "coder": "medium",
    "historian": "medium",
    "web-implementer": "medium",
    "investigator": "high",
    "reviewer": "high",
    "critic": "high",
    "validator": "high",
    "web-viewer": "high",
}

TURN_LIMITS = {"scout": 30, "advisor": 8, "coder": 100, "reviewer": 80, "investigator": 120}


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


def test_agent_default_efforts():
    for name, effort in DEFAULT_EFFORTS.items():
        assert f"effort: {effort}" in frontmatter(name), f"{name} should default to effort {effort}"
    # Fable's thinking is always on, and the lead is told not to set effort for it.
    assert not any(line.startswith("effort:") for line in frontmatter("advisor"))


def test_agent_turn_limits():
    """The limit is a backstop for the rule in preamble §6: a run that ignores
    the handoff rule is cut, not left to re-read 300k tokens per request."""
    for name, turns in TURN_LIMITS.items():
        assert f"maxTurns: {turns}" in frontmatter(name), f"{name} should be limited to {turns} turns"


def test_heavy_agents_carry_a_tool_allowlist():
    """Without an allowlist the first request carried ~52k chars of tool schemas
    (36k of them the `Artifact` tool these agents never use) against ~6k for scout."""
    wanted = {"coder": {"Edit", "Write"}, "reviewer": set(), "investigator": set()}
    for name, extra in wanted.items():
        line = next((x for x in frontmatter(name) if x.startswith("tools:")), None)
        assert line, f"{name} has no tools: allowlist"
        tools = line[len("tools:"):].strip().split(", ")
        assert not {"Agent", "Artifact"} & set(tools), f"{name} must not carry Agent/Artifact"
        assert {"Bash", "Read", "Skill", "SendMessage"} <= set(tools), f"{name} lost a basic tool"
        assert extra <= set(tools), f"{name} lost {extra - set(tools)}"
    reviewer = next(x for x in frontmatter("reviewer") if x.startswith("tools:"))
    assert "Edit" not in reviewer.split(", "), "reviewer reports, it does not fix"
