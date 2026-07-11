"""Contract tests for the chunker (no ML deps needed)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from singularity.search.chunking import extract_chunks, parse_frontmatter, should_skip

SAMPLE = """---
type: decision
title: "Тестовое решение"
tags: [alpha, beta]
aliases: [test-alias]
---

# Тестовое решение

Преамбула до первой секции, достаточно длинная чтобы пройти минимальный порог в пятьдесят символов.

## Контекст задачи

Это секция с [[Другая заметка|алиасом]] и ссылкой [текст](http://x) и **жирным** текстом, а также #тегом внутри. Длина этой секции точно превышает пятьдесят символов.

```python
code = "must be removed from content"
```

## Короткая

Мало.

## Русская секция про поиск

Семантический поиск должен находить русскоязычные заметки по смыслу запроса, а не по точному совпадению слов.
"""


def test_frontmatter():
    fm, body = parse_frontmatter(SAMPLE)
    assert fm["type"] == "decision"
    assert "alpha" in fm["tags"]
    assert body.lstrip().startswith("# Тестовое решение")


def test_chunks(tmp_path):
    p = tmp_path / "note.md"
    p.write_text(SAMPLE, encoding="utf-8")
    chunks = extract_chunks(p, "decisions/note.md")
    sections = [c["section"] for c in chunks]
    assert "Контекст задачи" in sections
    assert "Русская секция про поиск" in sections
    assert "Короткая" not in sections  # < 50 chars dropped

    ctx = next(c for c in chunks if c["section"] == "Контекст задачи")
    assert "must be removed" not in ctx["content"]       # code stripped
    assert "алиасом" in ctx["content"]                    # wikilink -> alias text
    assert "жирным" in ctx["content"]
    assert "Другая заметка" in ctx["wikilinks"]           # link target captured
    assert "тегом" in ctx["tags"]                          # inline tag merged
    assert "alpha" in ctx["tags"]                          # frontmatter tag merged
    assert ctx["type"] == "decision"
    assert ctx["text"].startswith("Тестовое решение / test-alias - Контекст задачи:")


def test_infer_type_from_path(tmp_path):
    p = tmp_path / "x.md"
    p.write_text("# T\n\n## S\n" + "х" * 60, encoding="utf-8")
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
    filler = ("Первое предложение секции с достаточным объёмом текста. " * 40)
    return (f"# Заголовок\n\n## Длинная секция\n\n{filler}\n\n{unique_tail}\n")


def test_long_section_windowed_not_truncated(tmp_path):
    p = tmp_path / "long.md"
    p.write_text(_long_note("УникальныйХвостовойМаркер42 в самом конце секции."),
                 encoding="utf-8")
    chunks = extract_chunks(p, "reference/long.md")
    joined = " ".join(c["content"] for c in chunks)
    assert "УникальныйХвостовойМаркер42" in joined      # tail survived (was dropped)
    assert not any(c["content"].endswith("...") for c in chunks)  # no truncation marker
    assert sum(1 for c in chunks if c["section"] == "Длинная секция") > 1  # multiple windows


def test_windows_overlap_preserves_context(tmp_path):
    p = tmp_path / "long.md"
    p.write_text(_long_note("Хвост."), encoding="utf-8")
    win = [c["content"] for c in extract_chunks(p, "reference/long.md")
           if c["section"] == "Длинная секция"]
    assert len(win) >= 2
    # end of window i shares text with start of window i+1
    tail = win[0][-80:]
    assert any(word in win[1] for word in tail.split()[:5])


def test_window_metadata_present_and_ordered(tmp_path):
    p = tmp_path / "long.md"
    p.write_text(_long_note("Хвост."), encoding="utf-8")
    win = [c for c in extract_chunks(p, "reference/long.md")
           if c["section"] == "Длинная секция"]
    assert [c["chunk_index"] for c in win] == list(range(len(win)))
    ids = [c["chunk_id"] for c in win]
    assert len(set(ids)) == len(ids)                    # stable + unique
    for c in win:
        assert c["line_start"] >= 1 and c["line_end"] >= c["line_start"]


def test_short_section_single_window(tmp_path):
    p = tmp_path / "note.md"
    p.write_text(SAMPLE, encoding="utf-8")
    ctx = [c for c in extract_chunks(p, "decisions/note.md")
           if c["section"] == "Контекст задачи"]
    assert len(ctx) == 1                                 # no regression for short sections
    assert ctx[0]["chunk_index"] == 0


def test_line_offsets_map_into_file(tmp_path):
    p = tmp_path / "note.md"
    p.write_text(SAMPLE, encoding="utf-8")
    total_lines = SAMPLE.count("\n") + 1
    rus = next(c for c in extract_chunks(p, "decisions/note.md")
               if c["section"] == "Русская секция про поиск")
    # the "## Русская секция про поиск" header is near the end of the file
    assert rus["line_start"] <= total_lines
    src_line = SAMPLE.splitlines().index("## Русская секция про поиск") + 1
    assert rus["line_start"] <= src_line <= rus["line_end"]
