"""Unit tests for install/client_registration — the shared safe-registration
primitives (GCLI-01). Direct, isolated coverage of the Markdown-block merge/remove,
JSON backup/parse, owned-hook merge/remove and path-ownership proof.
"""
import importlib.util
import json
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "client_registration", ENGINE / "install" / "client_registration.py")
cr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cr)

BEGIN = "<!-- X:BEGIN -->"
END = "<!-- X:END -->"
BLOCK = f"{BEGIN}\nmanaged body\n{END}"


# --- merge_managed_block -------------------------------------------------------

def test_merge_created(tmp_path):
    p = tmp_path / "sub" / "F.md"
    assert cr.merge_managed_block(p, BLOCK, BEGIN, END) == "created"
    assert p.read_text(encoding="utf-8") == BLOCK + "\n"


def test_merge_appended_preserves_user_text(tmp_path):
    p = tmp_path / "F.md"
    p.write_text("user note\n", encoding="utf-8")
    assert cr.merge_managed_block(p, BLOCK, BEGIN, END) == "appended"
    text = p.read_text(encoding="utf-8")
    assert text.startswith("user note")
    assert text.index("user note") < text.index(BEGIN)


def test_merge_refreshed_replaces_only_block(tmp_path):
    p = tmp_path / "F.md"
    p.write_text(f"top\n\n{BEGIN}\nSTALE\n{END}\n\nbottom\n", encoding="utf-8")
    assert cr.merge_managed_block(p, BLOCK, BEGIN, END) == "refreshed"
    text = p.read_text(encoding="utf-8")
    assert "STALE" not in text
    assert "top" in text and "bottom" in text
    assert text.count(BEGIN) == 1 and text.count(END) == 1


def test_merge_malformed_marker_falls_back_to_append(tmp_path):
    p = tmp_path / "F.md"
    p.write_text(f"user\n{BEGIN}\nhalf block\n", encoding="utf-8")  # no END
    assert cr.merge_managed_block(p, BLOCK, BEGIN, END) == "appended"
    assert "half block" in p.read_text(encoding="utf-8")


def test_merge_rerun_is_byte_stable(tmp_path):
    p = tmp_path / "F.md"
    cr.merge_managed_block(p, BLOCK, BEGIN, END)
    first = p.read_bytes()
    cr.merge_managed_block(p, BLOCK, BEGIN, END)
    assert p.read_bytes() == first


# --- remove_managed_block ------------------------------------------------------

def test_remove_keeps_user_text(tmp_path):
    p = tmp_path / "F.md"
    p.write_text(f"top\n\n{BEGIN}\nbody\n{END}\n\nbottom\n", encoding="utf-8")
    assert cr.remove_managed_block(p, BEGIN, END) is True
    text = p.read_text(encoding="utf-8")
    assert "top" in text and "bottom" in text
    assert BEGIN not in text and "body" not in text


def test_remove_deletes_file_when_only_block(tmp_path):
    p = tmp_path / "F.md"
    p.write_text(BLOCK + "\n", encoding="utf-8")
    assert cr.remove_managed_block(p, BEGIN, END) is True
    assert not p.exists()


def test_remove_noop_when_absent_or_unmarked(tmp_path):
    p = tmp_path / "F.md"
    assert cr.remove_managed_block(p, BEGIN, END) is False
    p.write_text("just user text\n", encoding="utf-8")
    assert cr.remove_managed_block(p, BEGIN, END) is False
    assert p.read_text(encoding="utf-8") == "just user text\n"


# --- backup_file ---------------------------------------------------------------

def test_backup_none_when_absent(tmp_path):
    assert cr.backup_file(tmp_path / "nope.json") is None


def test_backup_copies_bytes(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text('{"a":1}', encoding="utf-8")
    b = cr.backup_file(p)
    assert b is not None and b.exists()
    assert b.read_text(encoding="utf-8") == '{"a":1}'
    assert b.name.startswith("settings.json.bak-")


def test_backup_never_overwrites(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("v1", encoding="utf-8")
    b1 = cr.backup_file(p)
    p.write_text("v2", encoding="utf-8")
    b2 = cr.backup_file(p)          # same second -> suffixed, not overwritten
    assert b1 != b2
    assert b1.read_text(encoding="utf-8") == "v1"
    assert b2.read_text(encoding="utf-8") == "v2"


# --- read_json_or_raise --------------------------------------------------------

def test_read_json_missing_is_empty(tmp_path):
    assert cr.read_json_or_raise(tmp_path / "no.json") == {}


def test_read_json_valid(tmp_path):
    p = tmp_path / "s.json"
    p.write_text('{"k": [1, 2]}', encoding="utf-8")
    assert cr.read_json_or_raise(p) == {"k": [1, 2]}


def test_read_json_malformed_raises(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{ not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        cr.read_json_or_raise(p)


def test_read_json_tolerates_utf8_bom(tmp_path):
    """A UTF-8 BOM (Notepad/PowerShell on Windows) must not read as malformed."""
    p = tmp_path / "s.json"
    p.write_text('{"theme": "dark"}', encoding="utf-8-sig")   # writes BOM
    assert cr.read_json_or_raise(p) == {"theme": "dark"}


# --- merge_named_hooks / remove_named_hooks ------------------------------------

def _owned(name):
    return {"hooks": [{"type": "command", "command": "x", "name": name}]}


def _foreign(name="user-hook"):
    return {"hooks": [{"type": "command", "command": "y", "name": name}]}


def test_merge_adds_fresh_event(tmp_path):
    hooks = {}
    cr.merge_named_hooks(hooks, "SessionStart", [_owned("singularity-session-start")],
                         {"singularity-session-start"})
    assert len(hooks["SessionStart"]) == 1


def test_merge_replaces_owned_keeps_foreign(tmp_path):
    hooks = {"SessionStart": [_foreign(), _owned("singularity-session-start")]}
    cr.merge_named_hooks(hooks, "SessionStart", [_owned("singularity-session-start")],
                         {"singularity-session-start"})
    names = [h["name"] for d in hooks["SessionStart"] for h in d["hooks"]]
    assert names.count("singularity-session-start") == 1   # not duplicated
    assert "user-hook" in names                            # foreign preserved


def test_remove_named_hooks_drops_owned_only(tmp_path):
    hooks = {
        "SessionStart": [_foreign(), _owned("singularity-session-start")],
        "AfterAgent": [_owned("singularity-after-agent")],
    }
    removed = cr.remove_named_hooks(hooks, {"singularity-session-start", "singularity-after-agent"})
    assert removed == 2
    assert "AfterAgent" not in hooks                        # emptied event cleaned up
    remaining = [h["name"] for d in hooks["SessionStart"] for h in d["hooks"]]
    assert remaining == ["user-hook"]                       # foreign survives


# --- resolves_into -------------------------------------------------------------

def test_resolves_into_true_for_child(tmp_path):
    root = tmp_path / "engine"
    (root / "skills" / "s").mkdir(parents=True)
    assert cr.resolves_into(root / "skills" / "s", root) is True


def test_resolves_into_false_for_outsider(tmp_path):
    root = tmp_path / "engine"
    root.mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert cr.resolves_into(other, root) is False
