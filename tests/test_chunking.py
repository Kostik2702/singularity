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
