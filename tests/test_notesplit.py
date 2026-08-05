"""Gate for `mb note split` — the remedy for a note that outgrew its budget.

The bug this replaces was silent by construction: an over-budget brain note kept
loading its first 40 lines and dropping the rest, session after session, and the
only trace was a suffix that named no scale and no action. So the tests here
care about two things above correctness of formatting:

* nothing is lost in the split (the old failure, reproduced at a new layer);
* nothing is written unless the WHOLE plan is safe (a half-split vault is worse
  than an over-budget note — nobody can describe its state).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from singularity import cli  # noqa: E402
from singularity.config import Config  # noqa: E402
from singularity.knowledge import notesplit  # noqa: E402
from singularity.knowledge.context_map import read_note_excerpt  # noqa: E402

NOTE = """---
type: reference
title: "Singularity Brain"
tags: [singularity, brain]
project: singularity
status: active
---

# Singularity Brain

Что ассистент должен знать в каждой сессии этого проекта.

## Архитектура

Ядро на Python, тонкий платформенный слой.
Демон держит модель тёплой и слушает только 127.0.0.1.

## Гейты

Каждый чекбокс — исполняемый гейт.
Инвариант доказывается посадкой нарушения.

## Ловушки

Пайп поверх тестовой команды глотает exit-код.
"""


@pytest.fixture
def cfg(tmp_path):
    return Config(vault=tmp_path, data={"brain_max_lines": 6})


@pytest.fixture
def wired(monkeypatch, cfg):
    monkeypatch.setattr(cli, "load_config", lambda vault_arg=None: cfg)
    return cfg


@pytest.fixture
def brain(cfg):
    path = cfg.vault / "reference" / "Singularity Brain.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(NOTE, encoding="utf-8")
    return path


# --- planning is pure and complete -----------------------------------------

def test_plan_makes_one_child_per_section():
    p = notesplit.plan(NOTE, "reference/Singularity Brain.md", "2026-08-05", 6)
    assert p.ok
    assert [c.heading for c in p.children] == ["Архитектура", "Гейты", "Ловушки"]
    assert all(c.rel_path.startswith("reference/") for c in p.children)


def test_plan_preserves_every_line():
    """The invariant the whole feature exists to protect. Truncation lost the
    tail; a splitter that did the same would be the same bug with more steps."""
    p = notesplit.plan(NOTE, "reference/Singularity Brain.md", "2026-08-05", 6)
    assert notesplit.content_is_preserved(NOTE, p)
    joined = p.parent_text + "".join(c.text for c in p.children)
    assert "Пайп поверх тестовой команды глотает exit-код." in joined
    assert "Демон держит модель тёплой и слушает только 127.0.0.1." in joined


def test_plan_links_both_directions():
    """An orphaned child is worse than an over-budget note: it is knowledge
    with no way in."""
    p = notesplit.plan(NOTE, "reference/Singularity Brain.md", "2026-08-05", 6)
    for c in p.children:
        assert f"[[{c.stem}]]" in p.parent_text, f"parent does not link {c.stem}"
        assert "[[Singularity Brain]]" in c.text, f"{c.stem} does not link back"


def test_plan_index_is_smaller_than_the_source():
    p = notesplit.plan(NOTE, "reference/Singularity Brain.md", "2026-08-05", 6)
    assert p.parent_body_lines < p.source_body_lines


def test_children_inherit_type_and_project_but_not_status():
    p = notesplit.plan(NOTE, "reference/Singularity Brain.md", "2026-08-05", 6)
    text = p.children[0].text
    assert "type: reference" in text and "project: singularity" in text
    assert "status:" not in text, "status is the parent's editorial state, not the child's"
    assert "created: 2026-08-05" in text


def test_duplicate_headings_do_not_collide():
    note = "---\ntype: reference\ntitle: \"X\"\n---\n\n# X\n\n## Дубль\n\na\n\n## Дубль\n\nb\n"
    p = notesplit.plan(note, "reference/X.md", "2026-08-05", 5)
    stems = [c.stem for c in p.children]
    assert len(set(stems)) == len(stems), f"two files would share a name: {stems}"


def test_a_note_without_sections_is_refused_with_a_reason():
    note = "---\ntype: reference\ntitle: \"Flat\"\n---\n\n# Flat\n\njust prose, no headings\n"
    p = notesplit.plan(note, "reference/Flat.md", "2026-08-05", 5)
    assert not p.ok
    assert "nothing to split along" in p.error


# --- the CLI writes all or nothing -----------------------------------------

def test_split_writes_index_and_children(wired, cfg, brain, capsys):
    rc = cli.main(["note", "split", "reference/Singularity Brain.md"])
    assert rc == 0
    for name in ("Singularity Brain - Архитектура", "Singularity Brain - Гейты",
                 "Singularity Brain - Ловушки"):
        assert (cfg.vault / "reference" / f"{name}.md").is_file(), f"{name} not written"
    index = brain.read_text(encoding="utf-8")
    assert "## Разделы" in index
    # The FIRST line of a section survives as its index summary by design — that
    # is what tells the reader whether to open the link. Everything after it is
    # what must have moved out.
    assert "Ядро на Python" in index, "the index entry should summarise its child"
    assert "Демон держит модель тёплой" not in index, "detail stayed in the index"


def test_dry_run_writes_nothing(wired, cfg, brain, capsys):
    before = brain.read_text(encoding="utf-8")
    rc = cli.main(["note", "split", "reference/Singularity Brain.md", "--dry-run"])
    assert rc == 0
    assert brain.read_text(encoding="utf-8") == before
    assert not (cfg.vault / "reference" / "Singularity Brain - Гейты.md").exists()
    assert "dry run" in capsys.readouterr().out


def test_occupied_destination_aborts_before_any_write(wired, cfg, brain, capsys):
    """All-or-nothing: one taken name must not leave two children on disk and
    a rewritten parent pointing at a third that was never created."""
    clash = cfg.vault / "reference" / "Singularity Brain - Гейты.md"
    clash.write_text("mine\n", encoding="utf-8")
    before = brain.read_text(encoding="utf-8")

    rc = cli.main(["note", "split", "reference/Singularity Brain.md"])
    assert rc == 1
    assert "already exist" in capsys.readouterr().err
    assert clash.read_text(encoding="utf-8") == "mine\n", "clobbered a foreign note"
    assert brain.read_text(encoding="utf-8") == before, "parent rewritten anyway"
    assert not (cfg.vault / "reference" / "Singularity Brain - Архитектура.md").exists(), \
        "a child was written before the abort"


def test_missing_note_is_a_clean_error(wired, capsys):
    assert cli.main(["note", "split", "reference/Nope.md"]) == 1
    assert "no such note" in capsys.readouterr().err


# --- the truncation notice now names scale and remedy ----------------------

def test_truncation_suffix_names_the_scale_and_the_command(cfg, brain):
    """The old suffix said only that something was cut, which is why nobody
    ever acted on it. Both facts a reader needs are asserted here: how far over
    budget the note is, and what to run."""
    text = read_note_excerpt(cfg.vault, "reference/Singularity Brain.md", 6,
                             "Project brain (singularity)")
    assert text is not None
    assert "/6 lines" in text, "the suffix must state size against budget"
    assert "mb note split reference/Singularity Brain.md" in text, \
        "the suffix must name the remedy, not just the problem"


def test_no_suffix_when_the_note_fits(cfg):
    small = cfg.vault / "reference" / "Small.md"
    small.parent.mkdir(parents=True, exist_ok=True)
    small.write_text("---\ntype: reference\ntitle: \"S\"\n---\n\none line\n", encoding="utf-8")
    text = read_note_excerpt(cfg.vault, "reference/Small.md", 40, "Project brain (x)")
    assert text is not None and "truncated" not in text
