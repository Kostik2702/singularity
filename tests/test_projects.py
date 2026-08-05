"""T03: multi-project registry validation + pure cwd resolution.

detect_project/resolve_selector take plain dicts — no Config/CLI dependency —
so cross-platform path behavior (case-insensitive fs, symlinks, nesting) can
be pinned deterministically here without touching argv/config.json at all.
"""
import pytest

from singularity.machine import projects
from singularity.machine.projects import detect_project, resolve_selector, validate_projects


# --- validate_projects ---------------------------------------------------------

def test_absent_projects_key_is_valid():
    assert validate_projects({}) == {"errors": [], "warnings": []}


def test_single_valid_project_has_no_errors():
    res = validate_projects({"projects": {"singularity": {"path": "D:/Singularity"}}})
    assert res == {"errors": [], "warnings": []}


def test_projects_must_be_an_object():
    res = validate_projects({"projects": ["not", "a", "dict"]})
    assert any("projects" in e for e in res["errors"])


def test_reserved_id_general_rejected():
    res = validate_projects({"projects": {"general": {"path": "D:/x"}}})
    assert any("reserved" in e for e in res["errors"])


def test_reserved_id_personal_rejected():
    res = validate_projects({"projects": {"personal": {"path": "D:/x"}}})
    assert any("reserved" in e for e in res["errors"])


def test_entry_must_be_an_object():
    res = validate_projects({"projects": {"p": "not-a-dict"}})
    assert any("projects.p" in e for e in res["errors"])


def test_missing_path_is_an_error():
    res = validate_projects({"projects": {"p": {}}})
    assert any("path" in e for e in res["errors"])


def test_empty_path_is_an_error():
    res = validate_projects({"projects": {"p": {"path": "  "}}})
    assert any("path" in e for e in res["errors"])


def test_duplicate_path_across_two_ids_is_an_error():
    res = validate_projects({"projects": {
        "a": {"path": "D:/same"}, "b": {"path": "D:/same"}}})
    assert any("already registered" in e for e in res["errors"])


def test_group_colliding_with_reserved_id_is_an_error():
    res = validate_projects({"projects": {"p": {"path": "D:/x", "group": "personal"}}})
    assert any("reserved" in e for e in res["errors"])


def test_group_colliding_with_existing_project_id_is_an_error():
    res = validate_projects({"projects": {
        "a": {"path": "D:/a"}, "b": {"path": "D:/b", "group": "a"}}})
    assert any("id beats same-named group" in e for e in res["errors"])


def test_about_must_be_a_string():
    res = validate_projects({"projects": {"p": {"path": "D:/x", "about": 123}}})
    assert any("about" in e for e in res["errors"])


def test_unknown_entry_key_is_a_warning_not_error():
    res = validate_projects({"projects": {"p": {"path": "D:/x", "totally_unknown": 1}}})
    assert res["errors"] == []
    assert any("totally_unknown" in w for w in res["warnings"])


def test_invalid_id_type_is_an_error():
    res = validate_projects({"projects": {"": {"path": "D:/x"}}})
    assert any("invalid id" in e for e in res["errors"])


# --- detect_project --------------------------------------------------------------

def test_detect_returns_none_for_empty_registry():
    assert detect_project({}, "D:/anything") is None
    assert detect_project(None, "D:/anything") is None


def test_detect_exact_match(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    assert detect_project({"p": {"path": str(p)}}, str(p)) == "p"


def test_detect_deepest_match_wins(tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    reg = {"outer": {"path": str(outer)}, "inner": {"path": str(inner)}}
    assert detect_project(reg, str(inner / "sub")) == "inner"
    assert detect_project(reg, str(outer / "sibling")) == "outer"


def test_detect_no_match_returns_none(tmp_path):
    reg = {"p": {"path": str(tmp_path / "proj")}}
    assert detect_project(reg, str(tmp_path / "elsewhere")) is None


def test_detect_skips_malformed_entries(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    reg = {"broken": "not-a-dict", "empty": {}, "ok": {"path": str(p)}}
    assert detect_project(reg, str(p)) == "ok"


def test_detect_ignores_reserved_ids_even_if_present(tmp_path):
    reg = {"general": {"path": str(tmp_path)}}
    assert detect_project(reg, str(tmp_path)) is None


def test_detect_works_on_nonexistent_path():
    reg = {"p": {"path": "D:/does/not/exist/at/all"}}
    assert detect_project(reg, "D:/does/not/exist/at/all/sub") == "p"


def test_detect_case_insensitive_on_windows(monkeypatch, tmp_path):
    # tmp_path itself exists (resolvable prefix); the differing-case leaf
    # ("Proj" vs "PROJ") is deliberately never created on disk, so the only
    # source of case-folding under test is _normalize()'s platform check, not
    # a real OS case-insensitive filesystem lookup.
    monkeypatch.setattr(projects.sys, "platform", "win32")
    reg = {"p": {"path": str(tmp_path / "Proj")}}
    assert detect_project(reg, str(tmp_path / "PROJ")) == "p"


def test_detect_case_sensitive_on_linux(monkeypatch, tmp_path):
    monkeypatch.setattr(projects.sys, "platform", "linux")
    reg = {"p": {"path": str(tmp_path / "Proj")}}
    assert detect_project(reg, str(tmp_path / "PROJ")) is None


def test_detect_follows_symlink_to_real_target(tmp_path):
    real = tmp_path / "real_proj"
    real.mkdir()
    link = tmp_path / "link_proj"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not permitted on this machine")
    reg = {"p": {"path": str(link)}}
    assert detect_project(reg, str(real)) == "p"


# --- resolve_selector -----------------------------------------------------------

def test_resolve_reserved_ids_pass_through():
    assert resolve_selector({}, "general") == ["general"]
    assert resolve_selector({}, "personal") == ["personal"]


def test_resolve_exact_id_match():
    reg = {"a": {"path": "D:/a"}}
    assert resolve_selector(reg, "a") == ["a"]


def test_resolve_group_expands_to_all_members():
    reg = {"a": {"path": "D:/a", "group": "g"}, "b": {"path": "D:/b", "group": "g"},
          "c": {"path": "D:/c"}}
    assert resolve_selector(reg, "g") == ["a", "b"]


def test_resolve_id_beats_same_named_group():
    # 'a' is itself a project id; even if some OTHER project used group='a'
    # (which validate_projects would reject at write time), the resolver must
    # still prefer the id reading defensively over stale/invalid data.
    reg = {"a": {"path": "D:/a"}}
    assert resolve_selector(reg, "a") == ["a"]


def test_resolve_unknown_token_raises_with_known_ids_and_groups():
    reg = {"a": {"path": "D:/a", "group": "g"}}
    with pytest.raises(KeyError, match="unknown project/group 'nope'"):
        resolve_selector(reg, "nope")
