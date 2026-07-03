"""Markdown chunking for the vault index.

Ported from Genevra search-all.py::extract_memory_chunks (see vault:
Semantic_Search_Deep_Dive §3.2) with Obsidian extensions:
  * [[wikilinks]] / inline #tags / frontmatter aliases -> chunk metadata
  * aliases participate in the embedded text as title synonyms
  * callout markers (> [!note]) are stripped, text kept

Chunk = one `##` section; the embedded string is
"{title} - {section}: {content}" so headers carry semantics.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

MIN_CHUNK_CHARS = 50
MAX_CHUNK_CHARS = 500

_H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_SECTION_SPLIT_RE = re.compile(r"\n(?=##\s)")
_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_WIKILINK_RE = re.compile(r"\[\[([^\]\|#]+)(?:#[^\]\|]*)?(?:\|([^\]]+))?\]\]")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_CALLOUT_RE = re.compile(r"^>\s*\[![\w-]+\][+-]?\s*", re.MULTILINE)
_QUOTE_RE = re.compile(r"^>\s?", re.MULTILINE)
_INLINE_TAG_RE = re.compile(r"(?:^|\s)#([\w/\-а-яА-ЯёЁ]+)")
_WS_RE = re.compile(r"\s+")

# folder -> note type (Genevra _infer_type, adapted to the vault layout)
_TYPE_BY_FOLDER = {
    "learnings": "learning",
    "decisions": "decision",
    "howto": "howto",
    "incidents": "incident",
    "rules": "rule",
    "reference": "reference",
    "daily": "daily",
    "changelog": "changelog",
}


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter dict, body). Tolerates missing PyYAML (Genevra fallback)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    raw = text[3:end].strip("\n")
    body = text[end + 4:]
    try:
        import yaml

        data = yaml.safe_load(raw)
        return (data if isinstance(data, dict) else {}), body
    except Exception:
        fm: dict[str, Any] = {}
        for line in raw.splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                fm[key.strip()] = val.strip()
        return fm, body


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            value = value[1:-1]
        return [v.strip().strip("'\"") for v in value.split(",") if v.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value)]


def infer_type(rel_path: str, frontmatter: dict[str, Any]) -> str:
    if frontmatter.get("type"):
        return str(frontmatter["type"])
    first = rel_path.replace("\\", "/").split("/", 1)[0].lower()
    return _TYPE_BY_FOLDER.get(first, "reference")


def _clean_section(text: str) -> tuple[str, list[str], list[str]]:
    """Clean markdown noise; return (clean_text, wikilinks, inline_tags)."""
    text = _CODE_BLOCK_RE.sub(" ", text)
    text = text.replace("\\|", "|")  # escaped pipe in md tables
    wikilinks = [m.group(1).strip() for m in _WIKILINK_RE.finditer(text)]
    tags = [m.group(1) for m in _INLINE_TAG_RE.finditer(text)]
    text = _WIKILINK_RE.sub(lambda m: (m.group(2) or m.group(1)).strip(), text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _BOLD_RE.sub(r"\1", text)
    text = _CALLOUT_RE.sub("", text)
    text = _QUOTE_RE.sub("", text)
    text = _WS_RE.sub(" ", text).strip()
    return text, wikilinks, tags


def extract_chunks(path: Path, rel_path: str) -> list[dict[str, Any]]:
    """Chunk one markdown file. rel_path uses forward slashes."""
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    fm, body = parse_frontmatter(raw)

    m = _H1_RE.search(body)
    title = m.group(1).strip() if m else path.stem
    aliases = _as_list(fm.get("aliases"))
    fm_tags = _as_list(fm.get("tags"))
    note_type = infer_type(rel_path, fm)
    priority = fm.get("priority")

    title_part = title if not aliases else f"{title} / {', '.join(aliases)}"

    chunks: list[dict[str, Any]] = []
    for section_block in _SECTION_SPLIT_RE.split(body):
        section_block = section_block.strip("\n")
        if section_block.startswith("## "):
            header, _, rest = section_block.partition("\n")
            section = header[3:].strip()
        else:
            section = title  # preamble before first ## (Genevra: section 0)
            rest = section_block
        content, wikilinks, inline_tags = _clean_section(rest)
        if len(content) < MIN_CHUNK_CHARS:
            continue
        if len(content) > MAX_CHUNK_CHARS:
            content = content[:MAX_CHUNK_CHARS] + "..."
        all_tags = sorted({*fm_tags, *inline_tags})
        chunk: dict[str, Any] = {
            "file": rel_path,
            "title": title,
            "section": section,
            "content": content,
            "text": f"{title_part} - {section}: {content}",
            "type": note_type,
            "tags": ",".join(all_tags),
        }
        if wikilinks:
            chunk["wikilinks"] = sorted(set(wikilinks))
        if aliases:
            chunk["aliases"] = aliases
        if priority:
            chunk["priority"] = priority
        chunks.append(chunk)
    return chunks


def should_skip(rel_path: str, skip_dirs: list[str], skip_parts: list[str]) -> bool:
    p = rel_path.replace("\\", "/")
    first = p.split("/", 1)[0]
    if first in skip_dirs:
        return True
    return any(part in p for part in skip_parts)
