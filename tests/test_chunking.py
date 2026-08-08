"""Contract tests for the chunker (no ML deps needed)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from singularity.search.chunking import (extract_chunks, infer_project,
                                        parse_frontmatter, should_skip)

#: One section of this note is deliberately NOT in English. The chunker splits
#: on markdown structure and merges tags, aliases and wikilinks — none of which
#: may depend on the script the note is written in. An all-English fixture would
#: pass just as happily with a Latin-only regex, so it would stop testing the
#: property that matters ([[Rule - a claim of absence requires a control]]).
SAMPLE = """---
type: decision
title: "Test decision"
tags: [alpha, beta]
aliases: [test-alias]
---

# Test decision

A preamble before the first section, long enough to clear the fifty-character floor.

## Task context

This section has [[Another note|an alias]] and a link [text](http://x) and **bold**
text, plus an inline #tag. The length of this section is comfortably over fifty
characters.

```python
code = "must be removed from content"
```

## Short

Little.

## Секция не на латинице

Поиск обязан находить эту секцию по смыслу запроса, а её заголовок и теги —
переживать чанкинг без потерь, как и любая другая письменность.
"""


def test_frontmatter():
    fm, body = parse_frontmatter(SAMPLE)
    assert fm["type"] == "decision"
    assert "alpha" in fm["tags"]
    assert body.lstrip().startswith("# Test decision")


def test_chunks(tmp_path):
    p = tmp_path / "note.md"
    p.write_text(SAMPLE, encoding="utf-8")
    chunks = extract_chunks(p, "decisions/note.md")
    sections = [c["section"] for c in chunks]
    assert "Task context" in sections
    assert "Секция не на латинице" in sections, \
        "a non-Latin heading must survive chunking like any other"
    assert "Short" not in sections  # < 50 chars dropped

    ctx = next(c for c in chunks if c["section"] == "Task context")
    assert "must be removed" not in ctx["content"]       # code stripped
    assert "an alias" in ctx["content"]                   # wikilink -> alias text
    assert "bold" in ctx["content"]
    assert "Another note" in ctx["wikilinks"]             # link target captured
    assert "tag" in ctx["tags"]                            # inline tag merged
    assert "alpha" in ctx["tags"]                          # frontmatter tag merged
    assert ctx["type"] == "decision"
    assert ctx["text"].startswith("Test decision / test-alias - Task context:")


def test_infer_type_from_path(tmp_path):
    p = tmp_path / "x.md"
    p.write_text("# T\n\n## S\n" + "x" * 60, encoding="utf-8")
    chunks = extract_chunks(p, "howto/x.md")
    assert chunks[0]["type"] == "howto"


def test_should_skip():
    skip_dirs = [".obsidian", ".mb", "templates"]
    skip_parts = ["learnings/archive"]
    assert should_skip(".mb/config.md", skip_dirs, skip_parts)
    assert should_skip("learnings/archive/old.md", skip_dirs, skip_parts)
    assert not should_skip("learnings/active/T-1.md", skip_dirs, skip_parts)


# --- AUD-03: windowed chunking, no text loss --------------------------------

def _long_note(unique_tail):
    # a section whose content runs well past the old 500-char truncation point
    filler = ("The first sentence of the section, with enough text in it. " * 40)
    return (f"# Heading\n\n## Long section\n\n{filler}\n\n{unique_tail}\n")


def test_long_section_windowed_not_truncated(tmp_path):
    p = tmp_path / "long.md"
    p.write_text(_long_note("UniqueTailMarker42 at the very end of the section."),
                 encoding="utf-8")
    chunks = extract_chunks(p, "reference/long.md")
    joined = " ".join(c["content"] for c in chunks)
    assert "UniqueTailMarker42" in joined              # tail survived (was dropped)
    assert not any(c["content"].endswith("...") for c in chunks)  # no truncation marker
    assert sum(1 for c in chunks if c["section"] == "Long section") > 1  # multiple windows


def test_windows_overlap_preserves_context(tmp_path):
    p = tmp_path / "long.md"
    p.write_text(_long_note("Tail."), encoding="utf-8")
    win = [c["content"] for c in extract_chunks(p, "reference/long.md")
           if c["section"] == "Long section"]
    assert len(win) >= 2
    # end of window i shares text with start of window i+1
    tail = win[0][-80:]
    assert any(word in win[1] for word in tail.split()[:5])


def test_window_metadata_present_and_ordered(tmp_path):
    p = tmp_path / "long.md"
    p.write_text(_long_note("Tail."), encoding="utf-8")
    win = [c for c in extract_chunks(p, "reference/long.md")
           if c["section"] == "Long section"]
    assert [c["chunk_index"] for c in win] == list(range(len(win)))
    ids = [c["chunk_id"] for c in win]
    assert len(set(ids)) == len(ids)                    # stable + unique
    for c in win:
        assert c["line_start"] >= 1 and c["line_end"] >= c["line_start"]


def test_short_section_single_window(tmp_path):
    p = tmp_path / "note.md"
    p.write_text(SAMPLE, encoding="utf-8")
    ctx = [c for c in extract_chunks(p, "decisions/note.md")
           if c["section"] == "Task context"]
    assert len(ctx) == 1                                 # no regression for short sections
    assert ctx[0]["chunk_index"] == 0


# --- T04: project field from frontmatter -------------------------------------

def test_infer_project_explicit_value():
    assert infer_project({"project": "singularity"}) == "singularity"


def test_infer_project_absent_is_none():
    assert infer_project({}) is None


def test_infer_project_literal_general_normalizes_to_none():
    assert infer_project({"project": "general"}) is None
    assert infer_project({"project": "General"}) is None       # case-insensitive


def test_infer_project_whitespace_only_is_none():
    assert infer_project({"project": "   "}) is None


def test_infer_project_non_string_is_none():
    assert infer_project({"project": 123}) is None


def test_extract_chunks_stamps_project(tmp_path):
    p = tmp_path / "note.md"
    p.write_text("---\nproject: singularity\n---\n\n# T\n\n## S\n" + "x" * 60,
                 encoding="utf-8")
    chunks = extract_chunks(p, "reference/note.md")
    assert chunks[0]["project"] == "singularity"


def test_extract_chunks_omits_project_key_when_absent(tmp_path):
    p = tmp_path / "note.md"
    p.write_text("# T\n\n## S\n" + "x" * 60, encoding="utf-8")
    chunks = extract_chunks(p, "reference/note.md")
    assert "project" not in chunks[0]


def test_extract_chunks_omits_project_key_for_literal_general(tmp_path):
    p = tmp_path / "note.md"
    p.write_text("---\nproject: general\n---\n\n# T\n\n## S\n" + "x" * 60,
                 encoding="utf-8")
    chunks = extract_chunks(p, "reference/note.md")
    assert "project" not in chunks[0]


# --- T08: personal_roots retrofit ---------------------------------------------

def test_infer_project_personal_roots_match():
    assert infer_project({}, "journal/private/note.md", ["journal/private"]) == "personal"


def test_infer_project_personal_roots_exact_file_match():
    assert infer_project({}, "journal/one-file.md", ["journal/one-file.md"]) == "personal"


def test_infer_project_personal_roots_no_match():
    assert infer_project({}, "howto/x.md", ["journal/private"]) is None


def test_infer_project_frontmatter_wins_over_personal_roots():
    assert infer_project({"project": "singularity"}, "journal/private/note.md",
                         ["journal/private"]) == "singularity"


def test_infer_project_personal_roots_sibling_prefix_not_matched():
    # "journal/private" must not match "journal/privateish/x.md"
    assert infer_project({}, "journal/privateish/x.md", ["journal/private"]) is None


def test_infer_project_backslash_rel_path_normalized():
    assert infer_project({}, "journal\\private\\note.md", ["journal/private"]) == "personal"


def test_infer_project_empty_personal_roots_no_effect():
    assert infer_project({}, "journal/private/note.md", []) is None
    assert infer_project({}, "journal/private/note.md", None) is None


def test_extract_chunks_personal_roots_stamps_personal(tmp_path):
    d = tmp_path / "journal" / "private"; d.mkdir(parents=True)
    p = d / "note.md"
    p.write_text("# T\n\n## S\n" + "x" * 60, encoding="utf-8")
    chunks = extract_chunks(p, "journal/private/note.md", personal_roots=["journal/private"])
    assert chunks[0]["project"] == "personal"


def test_extract_chunks_without_personal_roots_arg_unaffected(tmp_path):
    d = tmp_path / "journal" / "private"; d.mkdir(parents=True)
    p = d / "note.md"
    p.write_text("# T\n\n## S\n" + "x" * 60, encoding="utf-8")
    chunks = extract_chunks(p, "journal/private/note.md")     # no personal_roots arg at all
    assert "project" not in chunks[0]


def test_line_offsets_map_into_file(tmp_path):
    p = tmp_path / "note.md"
    p.write_text(SAMPLE, encoding="utf-8")
    total_lines = SAMPLE.count("\n") + 1
    rus = next(c for c in extract_chunks(p, "decisions/note.md")
               if c["section"] == "Секция не на латинице")
    # the "## Секция не на латинице" header is near the end of the file
    assert rus["line_start"] <= total_lines
    src_line = SAMPLE.splitlines().index("## Секция не на латинице") + 1
    assert rus["line_start"] <= src_line <= rus["line_end"]
