"""W04: raw immutability (guard layer) + the note<-source provenance contract.

Two halves:
  * `immutable_roots` — the guard denies Write/Edit under `.mb/sources/` even
    in `audit` mode (D6), while every other guard check keeps "audit first".
  * provenance — `sources:` frontmatter, `mb note new --from-source`, the
    registry back-link, and two new (non-fatal) curator checks.
"""
from __future__ import annotations

import json

import pytest

from singularity import capture, curator, sources, vault_guard
from singularity.cli import main
from singularity.config import Config
from singularity.hooks import vault_guard as guard_hook


# --- guard core -------------------------------------------------------------

def test_immutable_root_matches_inside(tmp_path):
    assert vault_guard.immutable_root(
        tmp_path, tmp_path / ".mb" / "sources" / "2026" / "x.md") == ".mb/sources"


def test_immutable_root_matches_the_root_itself(tmp_path):
    assert vault_guard.immutable_root(tmp_path, tmp_path / ".mb" / "sources") == ".mb/sources"


def test_immutable_root_ignores_siblings(tmp_path):
    assert vault_guard.immutable_root(tmp_path, tmp_path / ".mb" / "sources_index" / "x") is None
    assert vault_guard.immutable_root(tmp_path, tmp_path / ".mb" / "config.json") is None


def test_immutable_root_applies_to_existing_files_too(tmp_path):
    """Unlike new_file_managed_root, an EXISTING source is exactly what must
    not be edited."""
    p = tmp_path / ".mb" / "sources" / "2026" / "x.md"
    p.parent.mkdir(parents=True)
    p.write_text("raw", encoding="utf-8")
    assert vault_guard.immutable_root(tmp_path, p) == ".mb/sources"


def test_dot_dot_traversal_does_not_escape(tmp_path):
    sneaky = tmp_path / "reference" / ".." / ".mb" / "sources" / "x.md"
    assert vault_guard.immutable_root(tmp_path, sneaky) == ".mb/sources"


def test_outside_the_vault_is_not_matched(tmp_path, tmp_path_factory):
    other = tmp_path_factory.mktemp("elsewhere")
    assert vault_guard.immutable_root(tmp_path, other / ".mb" / "sources" / "x.md") is None


def test_custom_roots_are_honored(tmp_path):
    assert vault_guard.immutable_root(tmp_path, tmp_path / "frozen" / "a.md",
                                      ["frozen"]) == "frozen"
    assert vault_guard.immutable_root(tmp_path, tmp_path / ".mb" / "sources" / "a.md",
                                      ["frozen"]) is None


# --- guard hook -------------------------------------------------------------

@pytest.fixture
def guarded_vault(tmp_path, monkeypatch):
    monkeypatch.setattr("singularity.config.resolve_vault", lambda _x=None: tmp_path)
    (tmp_path / ".mb").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _event(vault, rel, tool="Write"):
    return {"tool_name": tool, "tool_input": {"file_path": str(vault / rel)},
            "cwd": str(vault)}


def _enable(vault, **extra):
    (vault / ".mb" / "config.json").write_text(
        json.dumps({"vault_guard_enabled": True, **extra}), encoding="utf-8")


def test_hook_denies_source_edit_even_in_audit_mode(guarded_vault):
    _enable(guarded_vault, vault_guard_mode="audit")
    out = guard_hook.evaluate(_event(guarded_vault, ".mb/sources/2026/x.md"))
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "mb ingest add" in hso["permissionDecisionReason"]
    assert "mb ingest export" in hso["permissionDecisionReason"]


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
def test_every_guarded_tool_is_denied(guarded_vault, tool):
    _enable(guarded_vault)
    out = guard_hook.evaluate(_event(guarded_vault, ".mb/sources/2026/x.md", tool))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_is_still_a_no_op_when_the_guard_is_disabled(guarded_vault):
    """Honest scope: with vault_guard_enabled=false (the default) the ONLY
    real protection is write-once in the engine."""
    (guarded_vault / ".mb" / "config.json").write_text(
        json.dumps({"vault_guard_enabled": False}), encoding="utf-8")
    assert guard_hook.evaluate(_event(guarded_vault, ".mb/sources/2026/x.md")) is None


def test_custom_immutable_roots_from_config(guarded_vault):
    _enable(guarded_vault, immutable_roots=["frozen"])
    assert guard_hook.evaluate(_event(guarded_vault, "frozen/a.md")
                               )["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert guard_hook.evaluate(_event(guarded_vault, ".mb/sources/2026/x.md")) is None


def test_other_checks_keep_audit_first(guarded_vault):
    """Regression: the D6 exception must not leak into the pre-existing checks."""
    _enable(guarded_vault, vault_guard_mode="audit")
    out = guard_hook.evaluate(_event(guarded_vault, "learnings/active/NEW.md"))
    hso = out["hookSpecificOutput"]
    assert "permissionDecision" not in hso
    assert "additionalContext" in hso

    out = guard_hook.evaluate(_event(guarded_vault, ".mb/config.json"))
    assert "permissionDecision" not in out["hookSpecificOutput"]


def test_deny_mode_still_denies_the_pre_existing_checks(guarded_vault):
    _enable(guarded_vault, vault_guard_mode="deny")
    out = guard_hook.evaluate(_event(guarded_vault, "learnings/active/NEW.md"))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- frontmatter contract ---------------------------------------------------

def test_with_sources_frontmatter_inserts():
    body = '---\ntype: reference\ntitle: "X"\n---\n\n# X\n'
    out = capture.with_sources_frontmatter(body, ["abc123def456"])
    assert 'sources: ["abc123def456"]' in out.split("---")[1]


def test_with_sources_frontmatter_fills_an_empty_template_value():
    """`sources: []` from the shipped template must be FILLED, not skipped —
    otherwise --from-source would silently produce a note with no provenance."""
    body = '---\ntype: reference\nsources: []\ntitle: "X"\n---\n\n# X\n'
    out = capture.with_sources_frontmatter(body, ["abc123def456"])
    assert 'sources: ["abc123def456"]' in out
    assert "sources: []" not in out
    assert "title:" in out and "type: reference" in out


def test_with_sources_frontmatter_never_clobbers_a_real_value():
    body = '---\ntype: reference\nsources: ["already"]\n---\n\n# X\n'
    assert capture.with_sources_frontmatter(body, ["new"]) == body


def test_with_sources_frontmatter_no_ops_without_ids_or_frontmatter():
    body = '---\ntype: reference\n---\n\n# X\n'
    assert capture.with_sources_frontmatter(body, []) == body
    assert capture.with_sources_frontmatter("# no frontmatter\n", ["a"]) == "# no frontmatter\n"


def test_multiple_sources_are_all_recorded():
    body = '---\ntype: reference\n---\n\n# X\n'
    out = capture.with_sources_frontmatter(body, ["a1", "b2"])
    assert 'sources: ["a1", "b2"]' in out


# --- note_source_ids parsing ------------------------------------------------

@pytest.mark.parametrize("value, expect", [
    (["a", "b"], ["a", "b"]),
    ("a, b", ["a", "b"]),
    ('["a", "b"]', ["a", "b"]),
    (None, []),
    ([], []),
    ("", []),
])
def test_note_source_ids(value, expect):
    assert curator.note_source_ids({"sources": value}) == expect


# --- registry back-link -----------------------------------------------------

def test_attach_notes_merges_and_dedups(tmp_path):
    cfg = Config(vault=tmp_path, data={})
    sid = sources.add(cfg, "# D\n\nbody", origin_ref="ref")["source_id"]
    sources.attach_notes(cfg, sid, ["reference/a.md"])
    out = sources.attach_notes(cfg, sid, ["reference/a.md", "reference/b.md"])
    assert out["derived_notes"] == ["reference/a.md", "reference/b.md"]
    assert out["added"] == ["reference/b.md"]


def test_attach_notes_tolerates_a_missing_file(tmp_path):
    cfg = Config(vault=tmp_path, data={})
    sid = sources.add(cfg, "# D\n\nbody", origin_ref="ref")["source_id"]
    out = sources.attach_notes(cfg, sid, ["reference/deleted.md"], status="processed")
    assert out["derived_notes"] == ["reference/deleted.md"]
    assert out["status"] == "processed"


def test_attach_notes_rejects_unknown_id_and_status(tmp_path):
    cfg = Config(vault=tmp_path, data={})
    sid = sources.add(cfg, "# D\n\nbody", origin_ref="ref")["source_id"]
    with pytest.raises(sources.SourceError):
        sources.attach_notes(cfg, "deadbeefcafe", ["a.md"])
    with pytest.raises(sources.SourceError):
        sources.attach_notes(cfg, sid, ["a.md"], status="bogus")


# --- mb note new --from-source ----------------------------------------------

@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setenv("MB_HOME", str(tmp_path))
    return tmp_path


def _run(argv, capsys):
    code = main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_note_new_from_source_writes_both_directions(vault, tmp_path, capsys):
    doc = tmp_path / "doc.md"
    doc.write_text("# Doc\n\nsome external content here", encoding="utf-8")
    _, out, _ = _run(["ingest", "add", str(doc), "--json"], capsys)
    sid = json.loads(out)["source_id"]

    code, out, err = _run(["note", "new", "reference", "Derived", "--from-source", sid],
                          capsys)
    assert code == 0, err
    note = vault / "reference" / "Derived.md"
    assert f'sources: ["{sid}"]' in note.read_text(encoding="utf-8")

    cfg = Config(vault=vault, data={})
    assert sources.get(cfg, sid)["derived_notes"] == ["reference/Derived.md"]


def test_note_new_rejects_an_unknown_source_before_creating_anything(vault, capsys):
    code, _, err = _run(["note", "new", "reference", "Bad", "--from-source", "nope"],
                        capsys)
    assert code == 2 and "unknown source_id" in err
    assert not (vault / "reference" / "Bad.md").exists()


def test_note_new_without_from_source_is_unchanged(vault, capsys):
    code, _, _ = _run(["note", "new", "reference", "Plain"], capsys)
    assert code == 0
    assert "sources:" not in (vault / "reference" / "Plain.md").read_text(encoding="utf-8")


# --- curator checks ---------------------------------------------------------

def _note(vault, rel, fm_extra=""):
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f'---\ntype: reference\ntitle: "T"\ncreated: 2026-01-01\n{fm_extra}---\n\n'
                 f"# T\n\nbody\n", encoding="utf-8")
    return p


def test_dangling_source_reference_is_reported_without_changing_the_exit_code(tmp_path):
    cfg = Config(vault=tmp_path, data={})
    _note(tmp_path, "reference/a.md", 'sources: ["deadbeefcafe"]\n')
    issues = curator.validate(cfg)
    assert issues["source_not_in_registry"] == [
        {"file": "reference/a.md", "source_id": "deadbeefcafe"}]
    assert issues["broken_links"] == []
    assert curator.run(cfg, as_json=True) == 0


def test_known_source_reference_is_clean(tmp_path):
    cfg = Config(vault=tmp_path, data={})
    sid = sources.add(cfg, "# D\n\nbody", origin_ref="ref")["source_id"]
    _note(tmp_path, "reference/a.md", f'sources: ["{sid}"]\n')
    assert curator.validate(cfg)["source_not_in_registry"] == []


def test_orphan_source_after_the_grace_period(tmp_path):
    cfg = Config(vault=tmp_path, data={})
    fresh = sources.add(cfg, "# Fresh\n\nbody one", origin_ref="a")["source_id"]
    stale = sources.add(cfg, "# Stale\n\nbody two", origin_ref="b",
                        now="2020-01-01T00:00:00")["source_id"]
    orphans = {o["source_id"] for o in curator.validate(cfg)["orphan_source"]}
    assert orphans == {stale}
    assert fresh not in orphans


def test_processed_sources_are_never_orphans(tmp_path):
    cfg = Config(vault=tmp_path, data={})
    sid = sources.add(cfg, "# Old\n\nbody", origin_ref="a",
                      now="2020-01-01T00:00:00")["source_id"]
    sources.attach_notes(cfg, sid, ["reference/x.md"], status="processed")
    assert curator.validate(cfg)["orphan_source"] == []


def test_no_registry_means_no_provenance_issues(tmp_path):
    cfg = Config(vault=tmp_path, data={})
    _note(tmp_path, "reference/a.md")
    issues = curator.validate(cfg)
    assert issues["source_not_in_registry"] == [] and issues["orphan_source"] == []


def test_corrupt_registry_does_not_break_curator(tmp_path):
    cfg = Config(vault=tmp_path, data={})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    sources.registry_path(cfg).write_text("{broken", encoding="utf-8")
    _note(tmp_path, "reference/a.md", 'sources: ["x"]\n')
    issues = curator.validate(cfg)
    assert issues["source_not_in_registry"] == [{"file": "reference/a.md", "source_id": "x"}]
