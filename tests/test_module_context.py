"""T07: client-neutral module-context matching, marker, and note-excerpt core
(singularity/module_context.py). Client wire adapters are tested separately
in test_module_context_hook.py."""
import json

import pytest

from singularity import module_context


# --- match_module ------------------------------------------------------------

def test_no_match_on_empty_map():
    assert module_context.match_module({}, "hooks/x.py") is None
    assert module_context.match_module(None, "hooks/x.py") is None


def test_exact_file_glob_matches():
    cmap = {"hooks": {"globs": ["install/register_hooks.py"], "note": "n.md"}}
    assert module_context.match_module(cmap, "install/register_hooks.py") == "hooks"


def test_directory_glob_matches_files_inside():
    cmap = {"hooks": {"globs": ["singularity/hooks"], "note": "n.md"}}
    assert module_context.match_module(cmap, "singularity/hooks/stop_capture.py") == "hooks"


def test_directory_glob_does_not_match_sibling_prefix():
    # "singularity/hooks" must not match "singularity/hooksish/x.py"
    cmap = {"hooks": {"globs": ["singularity/hooks"], "note": "n.md"}}
    assert module_context.match_module(cmap, "singularity/hooksish/x.py") is None


def test_no_match_outside_any_glob():
    cmap = {"hooks": {"globs": ["singularity/hooks"], "note": "n.md"}}
    assert module_context.match_module(cmap, "tests/test_foo.py") is None


def test_longest_prefix_wins_on_collision():
    cmap = {
        "engine": {"globs": ["singularity"], "note": "engine.md"},
        "hooks": {"globs": ["singularity/hooks"], "note": "hooks.md"},
    }
    assert module_context.match_module(cmap, "singularity/hooks/stop_capture.py") == "hooks"
    assert module_context.match_module(cmap, "singularity/config.py") == "engine"


def test_backslash_paths_normalized():
    cmap = {"hooks": {"globs": ["singularity/hooks"], "note": "n.md"}}
    assert module_context.match_module(cmap, "singularity\\hooks\\stop_capture.py") == "hooks"


def test_malformed_entries_skipped_not_raised():
    cmap = {"a": "not-a-dict", "b": {"globs": "not-a-list"}, "c": {"globs": [123, None]},
           "ok": {"globs": ["hooks"], "note": "n.md"}}
    assert module_context.match_module(cmap, "hooks/x.py") == "ok"


def test_non_dict_context_map_returns_none():
    assert module_context.match_module(["not", "a", "dict"], "hooks/x.py") is None


# --- module_marker -------------------------------------------------------------

@pytest.fixture
def vault(tmp_path):
    return tmp_path


def test_marker_path_is_deterministic(vault):
    m1 = module_context.module_marker(vault, "claude", "s1", "singularity", "hooks")
    m2 = module_context.module_marker(vault, "claude", "s1", "singularity", "hooks")
    assert m1 == m2
    assert m1.parent == vault / ".mb" / "markers"


def test_marker_differs_per_module(vault):
    m1 = module_context.module_marker(vault, "claude", "s1", "p", "hooks")
    m2 = module_context.module_marker(vault, "claude", "s1", "p", "engine")
    assert m1 != m2


def test_marker_differs_per_session(vault):
    m1 = module_context.module_marker(vault, "claude", "s1", "p", "hooks")
    m2 = module_context.module_marker(vault, "claude", "s2", "p", "hooks")
    assert m1 != m2


def test_marker_sanitizes_unsafe_characters(vault):
    m = module_context.module_marker(vault, "claude", "../../etc", "p", "m")
    assert ".." not in m.name
    assert "/" not in m.name and "\\" not in m.name
    # never escapes the markers directory
    assert m.parent == vault / ".mb" / "markers"


def test_marker_creates_markers_dir(vault):
    module_context.module_marker(vault, "claude", "s1", "p", "hooks")
    assert (vault / ".mb" / "markers").is_dir()


# --- module_note_block -----------------------------------------------------------

def _write_context_json(vault, data):
    (vault / ".mb").mkdir(parents=True, exist_ok=True)
    (vault / ".mb" / "context.json").write_text(json.dumps(data), encoding="utf-8")


def test_module_note_block_reads_mapped_note(vault):
    ref = vault / "reference"; ref.mkdir(parents=True)
    (ref / "Hooks.md").write_text("Hook architecture facts.\n", encoding="utf-8")
    _write_context_json(vault, {"p": {"context_map": {
        "hooks": {"globs": ["singularity/hooks"], "note": "reference/Hooks.md"}}}})
    block = module_context.module_note_block(vault, "p", "hooks")
    assert "Hook architecture facts." in block
    assert "Module context (p/hooks)" in block


def test_module_note_block_none_when_no_entry(vault):
    assert module_context.module_note_block(vault, "p", "hooks") is None


def test_module_note_block_none_when_module_missing(vault):
    _write_context_json(vault, {"p": {"context_map": {}}})
    assert module_context.module_note_block(vault, "p", "hooks") is None


def test_module_note_block_none_when_note_missing_file(vault):
    _write_context_json(vault, {"p": {"context_map": {
        "hooks": {"globs": ["x"], "note": "reference/DoesNotExist.md"}}}})
    assert module_context.module_note_block(vault, "p", "hooks") is None


def test_module_note_block_respects_max_lines(vault):
    ref = vault / "reference"; ref.mkdir(parents=True)
    (ref / "Hooks.md").write_text("\n".join(f"fact {i}" for i in range(20)), encoding="utf-8")
    _write_context_json(vault, {"p": {"context_map": {
        "hooks": {"globs": ["x"], "note": "reference/Hooks.md"}}}})
    block = module_context.module_note_block(vault, "p", "hooks", max_lines=3)
    assert "fact 2" in block and "fact 3" not in block


def test_module_note_block_path_traversal_rejected(vault, tmp_path):
    outside = tmp_path.parent / "module-secret.md"
    outside.write_text("SECRET", encoding="utf-8")
    _write_context_json(vault, {"p": {"context_map": {
        "hooks": {"globs": ["x"], "note": "../module-secret.md"}}}})
    assert module_context.module_note_block(vault, "p", "hooks") is None
