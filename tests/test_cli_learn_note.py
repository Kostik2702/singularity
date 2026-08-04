"""T05: `mb learn --project/--head/--force` and `mb note new --project` CLI
wiring — backward compatibility, dedup skip/force, cross-project warning,
project inheritance into frontmatter."""
import json

import pytest

from singularity import cli
from singularity.config import Config
from singularity.search.chunking import parse_frontmatter


@pytest.fixture
def cfg(tmp_path):
    # FR-1: learn/note new resolve --project through the SAME registry-backed
    # resolver as search, so an explicit project must be registered first —
    # matching how `mb search --project` already requires a known id/group.
    return Config(vault=tmp_path, data={"projects": {
        "singularity": {"path": str(tmp_path / "singularity-repo")},
        "other": {"path": str(tmp_path / "other-repo")},
    }})


@pytest.fixture
def wired(monkeypatch, cfg):
    monkeypatch.setattr(cli, "load_config", lambda vault_arg=None: cfg)
    return cfg


# --- mb learn: backward compatibility -------------------------------------------

def test_learn_backward_compatible_no_new_flags(wired, cfg, capsys):
    rc = cli.main(["learn", "an insight", "--task", "T1"])
    assert rc == 0
    path = cfg.vault / "learnings" / "active" / "T1.md"
    assert "an insight" in path.read_text(encoding="utf-8")


def test_learn_default_task_is_general(wired, cfg):
    cli.main(["learn", "x"])
    assert (cfg.vault / "learnings" / "active" / "GENERAL.md").exists()


# --- mb learn: dedup ---------------------------------------------------------------

def test_learn_second_call_with_same_insight_is_skipped(wired, cfg, capsys):
    cli.main(["learn", "The daemon crashed on restart.", "--task", "T1"])
    capsys.readouterr()
    rc = cli.main(["learn", "the daemon crashed on restart", "--task", "T1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "duplicate skipped" in out
    text = (cfg.vault / "learnings" / "active" / "T1.md").read_text(encoding="utf-8")
    assert text.count("## ") == 1     # only the original heading, nothing appended


def test_learn_force_bypasses_dedup(wired, cfg):
    cli.main(["learn", "The daemon crashed on restart.", "--task", "T1"])
    cli.main(["learn", "The daemon crashed on restart.", "--task", "T1", "--force"])
    text = (cfg.vault / "learnings" / "active" / "T1.md").read_text(encoding="utf-8")
    assert text.count("## ") == 2


def test_learn_different_insight_is_not_skipped(wired, cfg):
    cli.main(["learn", "The daemon crashed on restart.", "--task", "T1"])
    cli.main(["learn", "Completely unrelated search ranking bug.", "--task", "T1"])
    text = (cfg.vault / "learnings" / "active" / "T1.md").read_text(encoding="utf-8")
    assert text.count("## ") == 2


# --- mb learn: --head --------------------------------------------------------------

def test_learn_head_appears_in_heading_line(wired, cfg):
    cli.main(["learn", "some insight", "--task", "T1", "--head", "Daemon crash root cause"])
    text = (cfg.vault / "learnings" / "active" / "T1.md").read_text(encoding="utf-8")
    heading_line = next(l for l in text.splitlines() if l.startswith("## "))
    assert "Daemon crash root cause" in heading_line


def test_learn_without_head_uses_bare_timestamp(wired, cfg):
    cli.main(["learn", "some insight", "--task", "T1"])
    text = (cfg.vault / "learnings" / "active" / "T1.md").read_text(encoding="utf-8")
    heading_line = next(l for l in text.splitlines() if l.startswith("## "))
    assert "—" not in heading_line


# --- mb learn: project binding -----------------------------------------------------

def test_learn_explicit_project_written_to_new_file_frontmatter(wired, cfg):
    cli.main(["learn", "x", "--task", "T1", "--project", "singularity"])
    text = (cfg.vault / "learnings" / "active" / "T1.md").read_text(encoding="utf-8")
    fm, _ = parse_frontmatter(text)
    assert fm["project"] == "singularity"


def test_learn_general_project_omits_frontmatter_key(wired, cfg):
    cli.main(["learn", "x", "--task", "T1", "--project", "general"])
    text = (cfg.vault / "learnings" / "active" / "T1.md").read_text(encoding="utf-8")
    fm, _ = parse_frontmatter(text)
    assert "project" not in fm


def test_learn_personal_requires_explicit_choice(wired, cfg, monkeypatch):
    # cwd never auto-resolves to personal — confirm no project registry entry
    # can accidentally produce it either.
    cli.main(["learn", "x", "--task", "T1"])          # no --project, no registry
    text = (cfg.vault / "learnings" / "active" / "T1.md").read_text(encoding="utf-8")
    fm, _ = parse_frontmatter(text)
    assert fm.get("project") != "personal"

    cli.main(["learn", "y", "--task", "T2", "--project", "personal"])
    text2 = (cfg.vault / "learnings" / "active" / "T2.md").read_text(encoding="utf-8")
    fm2, _ = parse_frontmatter(text2)
    assert fm2["project"] == "personal"


# --- T08: personal_roots retrofit end-to-end ---------------------------------------

def test_note_new_under_personal_root_auto_classified(wired, cfg):
    cfg.data["personal_roots"] = ["howto"]     # anything under howto/ is personal here
    cli.main(["note", "new", "howto", "Diary Entry"])
    path = cfg.vault / "howto" / "Diary Entry.md"
    fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert fm["project"] == "personal"


def test_note_new_outside_personal_root_unaffected(wired, cfg):
    cfg.data["personal_roots"] = ["howto"]
    cli.main(["note", "new", "reference", "Some Note"])
    path = cfg.vault / "reference" / "Some Note.md"
    fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert "project" not in fm


def test_learn_unknown_project_token_errors(wired, capsys):
    rc = cli.main(["learn", "x", "--task", "T1", "--project", "nope"])
    assert rc == 1
    assert "unknown project/group" in capsys.readouterr().err


def test_learn_cross_project_append_warns_but_does_not_lose_data(wired, cfg, capsys):
    cli.main(["learn", "first", "--task", "T1", "--project", "singularity"])
    capsys.readouterr()
    cli.main(["learn", "second unrelated note", "--task", "T1", "--project", "other"])
    err = capsys.readouterr().err
    assert "WARNING" in err and "singularity" in err
    text = (cfg.vault / "learnings" / "active" / "T1.md").read_text(encoding="utf-8")
    assert "first" in text and "second unrelated note" in text     # nothing lost
    fm, _ = parse_frontmatter(text)
    assert fm["project"] == "singularity"      # original frontmatter untouched


# --- mb note new: project inheritance ----------------------------------------------

def test_note_new_explicit_project(wired, cfg):
    cli.main(["note", "new", "howto", "Some Title", "--project", "singularity"])
    path = cfg.vault / "howto" / "Some Title.md"
    fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert fm["project"] == "singularity"


def test_note_new_no_project_flag_general_omitted(wired, cfg):
    cli.main(["note", "new", "howto", "Some Title"])
    path = cfg.vault / "howto" / "Some Title.md"
    fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert "project" not in fm


def test_note_new_unknown_project_errors_before_writing(wired, cfg, capsys):
    rc = cli.main(["note", "new", "howto", "Some Title", "--project", "nope"])
    assert rc == 1
    assert not (cfg.vault / "howto" / "Some Title.md").exists()


# --- mb note new: the filename is the wikilink target ------------------------
#
# The sanitizer, not the author, decides the filename: a comma becomes `_`, so
# `[[<title>]]` typed from memory in another note points at nothing. The break
# does not surface at creation — it surfaces later as a missing page in
# `mb curator gaps`, next to genuinely unwritten notes. Cost a rename and two
# link edits on 2026-08-04.

def test_note_new_warns_when_the_filename_differs(wired, cfg, capsys):
    title = "Rule - verify through the command, not the function"
    assert cli.main(["note", "new", "rule", title]) == 0
    err = capsys.readouterr().err
    assert "filename differs from the title" in err
    # The warning must quote the link target that actually works.
    assert "[[Rule - verify through the command_ not the function]]" in err
    assert (cfg.vault / "rules"
            / "Rule - verify through the command_ not the function.md").exists()


def test_note_new_is_quiet_when_the_title_survives(wired, cfg, capsys):
    assert cli.main(["note", "new", "rule", "Rule - plain title"]) == 0
    assert "filename differs" not in capsys.readouterr().err


def test_note_new_warning_does_not_change_the_frontmatter_title(wired, cfg):
    title = "Howto: run the suite"
    cli.main(["note", "new", "howto", title])
    body = (cfg.vault / "howto" / "Howto_ run the suite.md").read_text(encoding="utf-8")
    fm, _ = parse_frontmatter(body)
    assert fm["title"] == title      # the title is the author's, only the path is sanitized
