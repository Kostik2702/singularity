"""T06: `.mb/context.json` loader + project brain block builder."""
import json

import pytest

from singularity.knowledge import context_map


@pytest.fixture
def vault(tmp_path):
    (tmp_path / ".mb").mkdir(parents=True)
    return tmp_path


def _write_context(vault, data):
    (vault / ".mb" / "context.json").write_text(json.dumps(data), encoding="utf-8")


# --- load_context_map ------------------------------------------------------------

def test_missing_file_returns_empty(vault):
    assert context_map.load_context_map(vault) == {}


def test_malformed_json_returns_empty(vault):
    (vault / ".mb" / "context.json").write_text("{not json", encoding="utf-8")
    assert context_map.load_context_map(vault) == {}


def test_non_object_root_returns_empty(vault):
    _write_context(vault, ["not", "an", "object"])
    assert context_map.load_context_map(vault) == {}


def test_valid_map_loaded(vault):
    _write_context(vault, {"p": {"brain_note": "reference/Brain.md"}})
    assert context_map.load_context_map(vault) == {"p": {"brain_note": "reference/Brain.md"}}


# --- project_brain_block: absence/no-op cases -------------------------------------

def test_no_project_returns_none(vault):
    assert context_map.project_brain_block(vault, None) is None


def test_no_context_json_returns_none(vault):
    assert context_map.project_brain_block(vault, "p") is None


def test_no_entry_for_project_returns_none(vault):
    _write_context(vault, {"other": {"brain_note": "reference/Brain.md"}})
    assert context_map.project_brain_block(vault, "p") is None


def test_no_brain_note_key_returns_none(vault):
    _write_context(vault, {"p": {}})
    assert context_map.project_brain_block(vault, "p") is None


def test_brain_note_pointing_to_missing_file_returns_none(vault):
    _write_context(vault, {"p": {"brain_note": "reference/Nope.md"}})
    assert context_map.project_brain_block(vault, "p") is None


# --- project_brain_block: real content --------------------------------------------

def _write_brain_note(vault, lines, frontmatter=True):
    d = vault / "reference"
    d.mkdir(parents=True, exist_ok=True)
    body = "\n".join(lines)
    if frontmatter:
        body = "---\ntype: reference\ntitle: \"Brain\"\n---\n\n" + body
    (d / "Brain.md").write_text(body, encoding="utf-8")


def test_reads_and_strips_frontmatter(vault):
    _write_brain_note(vault, ["# Brain", "", "Key fact one."])
    _write_context(vault, {"p": {"brain_note": "reference/Brain.md"}})
    block = context_map.project_brain_block(vault, "p")
    assert "Key fact one." in block
    assert "type: reference" not in block


def test_header_has_real_absolute_read_hint(vault):
    _write_brain_note(vault, ["content"])
    _write_context(vault, {"p": {"brain_note": "reference/Brain.md"}})
    block = context_map.project_brain_block(vault, "p")
    absolute = str((vault / "reference" / "Brain.md").resolve())
    assert f"Read {absolute}" in block


def test_truncates_to_max_lines_with_count(vault):
    _write_brain_note(vault, [f"line {i}" for i in range(20)])
    _write_context(vault, {"p": {"brain_note": "reference/Brain.md"}})
    block = context_map.project_brain_block(vault, "p", max_lines=5)
    assert "line 4" in block
    assert "line 5" not in block
    assert "15 more line" in block


def test_no_truncation_note_when_under_budget(vault):
    _write_brain_note(vault, ["one line"])
    _write_context(vault, {"p": {"brain_note": "reference/Brain.md"}})
    block = context_map.project_brain_block(vault, "p", max_lines=40)
    assert "more line" not in block


def test_invalid_max_lines_falls_back_to_default(vault):
    _write_brain_note(vault, [f"line {i}" for i in range(60)])
    _write_context(vault, {"p": {"brain_note": "reference/Brain.md"}})
    block = context_map.project_brain_block(vault, "p", max_lines=-5)
    assert f"line {context_map.DEFAULT_BRAIN_MAX_LINES - 1}" in block
    assert f"line {context_map.DEFAULT_BRAIN_MAX_LINES}" not in block


def test_bom_and_unicode_content_handled(vault):
    d = vault / "reference"; d.mkdir(parents=True)
    (d / "Brain.md").write_bytes(
        ("---\ntype: reference\n---\n\nЮникод-факт про проект.\n").encode("utf-8-sig"))
    _write_context(vault, {"p": {"brain_note": "reference/Brain.md"}})
    block = context_map.project_brain_block(vault, "p")
    assert "Юникод-факт" in block


# --- path traversal / security ----------------------------------------------------

def test_dotdot_traversal_outside_vault_rejected(vault, tmp_path):
    secret = tmp_path.parent / "outside-secret.md"
    secret.write_text("top secret", encoding="utf-8")
    _write_context(vault, {"p": {"brain_note": "../outside-secret.md"}})
    assert context_map.project_brain_block(vault, "p") is None


def test_absolute_path_escape_rejected(vault, tmp_path):
    outside = tmp_path.parent / "outside2.md"
    outside.write_text("nope", encoding="utf-8")
    _write_context(vault, {"p": {"brain_note": str(outside)}})
    assert context_map.project_brain_block(vault, "p") is None


def test_empty_brain_note_string_rejected(vault):
    _write_context(vault, {"p": {"brain_note": "   "}})
    assert context_map.project_brain_block(vault, "p") is None


def test_non_string_brain_note_rejected(vault):
    _write_context(vault, {"p": {"brain_note": 123}})
    assert context_map.project_brain_block(vault, "p") is None
