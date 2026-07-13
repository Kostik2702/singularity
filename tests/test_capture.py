"""T05: bounded dedup + project binding for `mb learn`/`mb note new`."""
import pytest

from singularity import capture
from singularity.config import Config


# --- normalize_gist --------------------------------------------------------------

def test_normalize_gist_lowercases_and_strips_punctuation():
    assert capture.normalize_gist("Hello, World!!") == "hello world"


def test_normalize_gist_collapses_whitespace():
    assert capture.normalize_gist("a   b\n\tc") == "a b c"


# --- find_duplicate ---------------------------------------------------------------

def test_no_duplicate_on_empty_file():
    assert capture.find_duplicate("", "some insight") is None


def test_exact_gist_match_is_a_duplicate():
    existing = "# T\n\n## 2026-01-01 10:00\nThe daemon crashed on restart.\n"
    dup = capture.find_duplicate(existing, "The daemon crashed on restart.")
    assert dup is not None and dup.heading == "2026-01-01 10:00"


def test_punctuation_and_case_normalization_still_matches():
    existing = "## 2026-01-01 10:00\nThe daemon crashed on restart!\n"
    dup = capture.find_duplicate(existing, "the DAEMON crashed, on restart")
    assert dup is not None


def test_retyped_near_copy_with_extra_trailing_text_matches():
    existing = "## 2026-01-01 10:00\nThe daemon crashed on restart due to a bad port.\n"
    # near-identical but with a small addition -> SequenceMatcher should still catch it
    dup = capture.find_duplicate(existing,
                                 "The daemon crashed on restart due to a bad port config.")
    assert dup is not None


def test_normalized_prefix_containment_matches():
    existing = "## 2026-01-01 10:00\nShort insight text here.\n"
    dup = capture.find_duplicate(existing, "Short insight text here. Plus a lot more detail "
                                            "added on a retype that changes everything below.")
    assert dup is not None


def test_unrelated_content_is_not_a_duplicate():
    existing = "## 2026-01-01 10:00\nThe daemon crashed on restart.\n"
    assert capture.find_duplicate(existing, "Completely different topic about search ranking.") is None


# 11 topically unrelated sentences so SequenceMatcher can't cross-match them —
# only an exact/near-retype of the SAME sentence should ever match.
_TOPICS = [
    "The daemon crashed on restart due to a bad port.",
    "Semantic search returns unrelated notes for short queries.",
    "The watcher heartbeat file was never written on this machine.",
    "Config validation rejects negative batch sizes correctly.",
    "Backup restore verified checksums against the manifest.",
    "The scheduler task registered with the wrong working directory.",
    "Encoder warm-up takes about two seconds on a cold daemon start.",
    "Gemini hooks require a minimum CLI version to activate.",
    "Codex profile merge preserves the user's own AGENTS notes.",
    "Index generation pruning kept only the last two committed folders.",
    "Doctor block E skips gracefully when Gemini is not installed.",
]


def test_beyond_window_duplicate_is_not_caught():
    # 11 sections; the true duplicate is the OLDEST (position 0), outside the
    # default 10-section window -> must NOT be detected (bounded, not full-scan).
    sections = "".join(f"## 2026-01-{i:02d} 10:00\n{topic}\n"
                       for i, topic in enumerate(_TOPICS, start=1))
    dup = capture.find_duplicate(sections, _TOPICS[0])
    assert dup is None


def test_within_window_duplicate_is_caught():
    sections = "".join(f"## 2026-01-{i:02d} 10:00\n{topic}\n"
                       for i, topic in enumerate(_TOPICS, start=1))
    dup = capture.find_duplicate(sections, _TOPICS[4])       # 5th topic, within last-10 window
    assert dup is not None and "2026-01-05" in dup.heading


def test_custom_window_size_is_respected():
    sections = "".join(f"## h{i}\nbody {i}\n" for i in range(5))
    assert capture.find_duplicate(sections, "body 0", window=3) is None    # outside window=3
    assert capture.find_duplicate(sections, "body 4", window=3) is not None


def test_empty_new_content_never_matches():
    existing = "## h\nsomething\n"
    assert capture.find_duplicate(existing, "   ") is None


# --- resolve_capture_project -------------------------------------------------------

def test_explicit_project_id_wins(tmp_path):
    cfg = Config(vault=tmp_path, data={"projects": {"p": {"path": "D:/p"}}})
    project, err = capture.resolve_capture_project(cfg, "p")
    assert project == "p" and err is None


def test_explicit_general_normalizes_to_none(tmp_path):
    cfg = Config(vault=tmp_path, data={})
    project, err = capture.resolve_capture_project(cfg, "general")
    assert project is None and err is None


def test_explicit_personal_passes_through(tmp_path):
    cfg = Config(vault=tmp_path, data={})
    project, err = capture.resolve_capture_project(cfg, "personal")
    assert project == "personal" and err is None


def test_explicit_group_token_is_rejected(tmp_path):
    cfg = Config(vault=tmp_path, data={"projects": {
        "a": {"path": "D:/a", "group": "g"}, "b": {"path": "D:/b", "group": "g"}}})
    project, err = capture.resolve_capture_project(cfg, "g")
    assert project is None
    assert err is not None and "group" in err


def test_explicit_unknown_token_is_rejected(tmp_path):
    cfg = Config(vault=tmp_path, data={})
    project, err = capture.resolve_capture_project(cfg, "nope")
    assert project is None
    assert err is not None and "unknown project/group" in err


def test_no_explicit_falls_back_to_cwd_detection(tmp_path, monkeypatch):
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    cfg = Config(vault=tmp_path, data={"projects": {"p": {"path": str(proj_dir)}}})
    monkeypatch.setattr(capture.Path, "cwd", classmethod(lambda cls: proj_dir))
    project, err = capture.resolve_capture_project(cfg, None)
    assert project == "p" and err is None


def test_no_explicit_and_no_match_is_none_never_personal(tmp_path, monkeypatch):
    cfg = Config(vault=tmp_path, data={})
    monkeypatch.setattr(capture.Path, "cwd", classmethod(lambda cls: tmp_path / "elsewhere"))
    project, err = capture.resolve_capture_project(cfg, None)
    assert project is None and err is None    # never auto-resolves to "personal"


# --- T08: personal_roots retrofit ---------------------------------------------

def test_dest_under_personal_root_resolves_to_personal(tmp_path, monkeypatch):
    cfg = Config(vault=tmp_path, data={"personal_roots": ["journal/private"]})
    monkeypatch.setattr(capture.Path, "cwd", classmethod(lambda cls: tmp_path / "elsewhere"))
    project, err = capture.resolve_capture_project(cfg, None, "journal/private/note.md")
    assert project == "personal" and err is None


def test_dest_outside_personal_root_falls_back_to_cwd_detection(tmp_path, monkeypatch):
    proj_dir = tmp_path / "proj"; proj_dir.mkdir()
    cfg = Config(vault=tmp_path, data={"personal_roots": ["journal/private"],
                                       "projects": {"p": {"path": str(proj_dir)}}})
    monkeypatch.setattr(capture.Path, "cwd", classmethod(lambda cls: proj_dir))
    project, err = capture.resolve_capture_project(cfg, None, "howto/note.md")
    assert project == "p" and err is None


def test_explicit_project_wins_over_personal_roots(tmp_path):
    cfg = Config(vault=tmp_path, data={"personal_roots": ["journal/private"],
                                       "projects": {"p": {"path": "D:/p"}}})
    project, err = capture.resolve_capture_project(cfg, "p", "journal/private/note.md")
    assert project == "p" and err is None


def test_no_dest_rel_path_skips_personal_roots_check(tmp_path, monkeypatch):
    cfg = Config(vault=tmp_path, data={"personal_roots": ["journal/private"]})
    monkeypatch.setattr(capture.Path, "cwd", classmethod(lambda cls: tmp_path / "elsewhere"))
    project, err = capture.resolve_capture_project(cfg, None)      # no dest_rel_path at all
    assert project is None and err is None


# --- with_project_frontmatter -------------------------------------------------------

def test_inserts_project_into_frontmatter():
    body = "---\ntype: learning\ntitle: \"x\"\n---\n\n# x\n"
    out = capture.with_project_frontmatter(body, "singularity")
    assert "project: singularity" in out
    assert out.count("---") == 2


def test_noop_when_project_is_none():
    body = "---\ntype: learning\n---\n\n# x\n"
    assert capture.with_project_frontmatter(body, None) == body


def test_noop_when_no_frontmatter_fence():
    body = "# x\n\nno frontmatter here\n"
    assert capture.with_project_frontmatter(body, "p") == body


def test_does_not_clobber_existing_project_key():
    body = "---\ntype: learning\nproject: already-set\n---\n\n# x\n"
    out = capture.with_project_frontmatter(body, "different")
    assert "project: already-set" in out
    assert "project: different" not in out
