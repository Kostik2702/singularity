"""Every skill and agent the engine ships has a frontmatter the harness can read.

An unquoted description that holds `word: ` is not YAML. The harness does not
report that: it drops the whole block, shows the first body line as the
description and ignores every other key. Seven skills shipped that way, so the
model chose them by their heading instead of their trigger list, and a flag such
as `disable-model-invocation` would have sat in the file unread.
"""
from pathlib import Path

import pytest
import yaml

ENGINE = Path(__file__).resolve().parents[1]
SOURCES = sorted((ENGINE / "singularity" / "skills").glob("*/SKILL.md")) + sorted(
    p for p in (ENGINE / "singularity" / "agents").glob("*.md") if p.name != "AGENT_SHARED_PREAMBLE.md"
)


def _block(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---", f"{path.name}: no frontmatter"
    return "\n".join(lines[1:lines.index("---", 1)])


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_frontmatter_is_a_yaml_mapping_with_a_description(path):
    meta = yaml.safe_load(_block(path))
    assert isinstance(meta, dict), f"{path}: frontmatter is not a mapping"
    assert isinstance(meta.get("name"), str) and meta["name"], f"{path}: no name"
    assert isinstance(meta.get("description"), str) and len(meta["description"]) > 20, f"{path}: no description"


def test_the_sweep_sees_every_source():
    """Control for the absence check above: an empty glob would pass it."""
    assert len(SOURCES) >= 40
