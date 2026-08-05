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
from typing import Any

from ..contracts import Chunk

MIN_CHUNK_CHARS = 50
# A long section is split into overlapping windows instead of being truncated
# (AUD-03: content past MAX_CHUNK_CHARS used to be silently dropped from the
# index). Window ~1000 chars, ~150 overlap so context spans the seam.
WINDOW_CHARS = 1000
WINDOW_OVERLAP = 150
# Bumped whenever the chunk shape changes so an old-format index auto-rebuilds.
CHUNK_SCHEMA_VERSION = 3

_H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_SENT_END_RE = re.compile(r"[.!?…](?:\s|$)")
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


def _under_root(rel_path: str, root: str) -> bool:
    rel_norm = rel_path.replace("\\", "/").strip("/")
    root_norm = str(root).replace("\\", "/").strip("/")
    if not root_norm:
        return False
    return rel_norm == root_norm or rel_norm.startswith(root_norm + "/")


def infer_project(frontmatter: dict[str, Any], rel_path: str = "",
                  personal_roots: list[str] | None = None) -> str | None:
    """T04 §8.2 classification, extended by T08's personal_roots retrofit
    (Open Decision, 2026-07-12: personal = frontmatter OR configured
    personal_roots — broader than T04's original frontmatter-only cut):

    1. Explicit `frontmatter.project` wins. A literal "general" normalizes to
       None (absent), matching the classify-as-general default so the compact
       label can omit `@general` unconditionally.
    2. Else, if `rel_path` falls under a configured `personal_roots` entry
       (vault-relative, forward-slash normalized) -> "personal".
    3. Else None (general) — a note's location otherwise carries no project
       meaning (unlike `type`, which folder-infers)."""
    value = frontmatter.get("project")
    if isinstance(value, str):
        value = value.strip()
        if value and value.lower() != "general":
            return value
    if rel_path and personal_roots:
        for root in personal_roots:
            if isinstance(root, str) and _under_root(rel_path, root):
                return "personal"
    return None


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


def _best_break(content: str, start: int, end: int) -> int:
    """Pick a cut point in (start, end] that lands on a sentence boundary, else
    a word boundary, else the hard limit — searching only the window's tail so
    windows stay near the target size."""
    floor = start + int((end - start) * 0.6)
    last = -1
    for m in _SENT_END_RE.finditer(content, floor, end):
        last = m.end()
    if last != -1:
        return last
    sp = content.rfind(" ", floor, end)
    return sp + 1 if sp != -1 else end


def _window_text(content: str,
                 size: int = WINDOW_CHARS, overlap: int = WINDOW_OVERLAP) -> list[str]:
    """Split content into overlapping windows covering 100% of it. Content that
    already fits returns a single window unchanged (short sections don't
    regress). Every non-empty window is kept — including a small trailing one —
    so no text is lost."""
    if len(content) <= size:
        return [content]
    windows: list[str] = []
    start, n = 0, len(content)
    while start < n:
        end = min(start + size, n)
        if end < n:
            end = _best_break(content, start, end)
        piece = content[start:end].strip()
        if piece:
            windows.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return windows


def extract_chunks(path: Path, rel_path: str,
                   personal_roots: list[str] | None = None) -> list[Chunk]:
    """Chunk one markdown file. rel_path uses forward slashes. personal_roots
    (T08 retrofit) is optional/default-None for callers indifferent to it."""
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
    project = infer_project(fm, rel_path, personal_roots)

    title_part = title if not aliases else f"{title} / {', '.join(aliases)}"

    # File line where the body (post-frontmatter) begins, so chunk line offsets
    # are 1-based line numbers in the actual file.
    body_line_offset = raw.count("\n") - body.count("\n")

    chunks: list[dict[str, Any]] = []
    cur_line = body_line_offset
    for section_ord, block in enumerate(_SECTION_SPLIT_RE.split(body)):
        line_start = cur_line + 1                 # 1-based, inclusive
        line_end = cur_line + block.count("\n") + 1
        cur_line = line_end                       # +1 separator "\n" consumed by split

        section_block = block.strip("\n")
        if section_block.startswith("## "):
            header, _, rest = section_block.partition("\n")
            section = header[3:].strip()
        else:
            section = title  # preamble before first ## (Genevra: section 0)
            rest = section_block
        content, wikilinks, inline_tags = _clean_section(rest)
        if len(content) < MIN_CHUNK_CHARS:
            continue
        all_tags = sorted({*fm_tags, *inline_tags})
        windows = _window_text(content)
        for wi, wtext in enumerate(windows):
            chunk: Chunk = {
                "file": rel_path,
                "title": title,
                "section": section,
                "content": wtext,
                "text": f"{title_part} - {section}: {wtext}",
                "type": note_type,
                "tags": ",".join(all_tags),
                "chunk_index": wi,          # window ordinal within the section
                "line_start": line_start,   # section line span in the file
                "line_end": line_end,
                "chunk_id": f"{rel_path}#{section_ord}.{wi}",
            }
            if wikilinks:
                chunk["wikilinks"] = sorted(set(wikilinks))
            if aliases:
                chunk["aliases"] = aliases
            if priority:
                chunk["priority"] = priority
            if project:
                chunk["project"] = project
            chunks.append(chunk)
    return chunks


def extract_source_chunks(path: Path, rel_path: str,
                          personal_roots: list[str] | None = None) -> list[Chunk]:
    """Chunker for the ingested-source corpus (WIKI-LOOP D2).

    Identical windowing to the vault chunker — the two corpora must be
    comparable by cosine, so they cannot chunk differently — with two
    additions: `type` is pinned to "source" (a raw document whose own
    frontmatter says `type: reference` must never masquerade as a note), and
    the owning `source_id` is copied from the wrapper frontmatter onto every
    chunk so `mb ingest plan` can select one document's rows without a second
    pass over disk."""
    chunks = extract_chunks(path, rel_path, personal_roots)
    if not chunks:
        return chunks
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return chunks
    fm, _ = parse_frontmatter(raw)
    sid = fm.get("source_id")
    sid = str(sid) if sid not in (None, "") else ""
    for chunk in chunks:
        chunk["type"] = "source"
        if sid:
            chunk["source_id"] = sid
    return chunks


def should_skip(rel_path: str, skip_dirs: list[str], skip_parts: list[str]) -> bool:
    p = rel_path.replace("\\", "/")
    first = p.split("/", 1)[0]
    if first in skip_dirs:
        return True
    return any(part in p for part in skip_parts)
