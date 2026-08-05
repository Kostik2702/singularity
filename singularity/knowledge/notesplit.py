"""Split a note that outgrew its injection budget into an index plus children.

A project-brain note is read into every session of that project, so it lives
under `brain_max_lines`. Past that line the excerpt builder truncates — and the
old behaviour ended there: the note kept growing, the tail kept not loading, and
the only signal was a suffix nobody had a reason to act on.

Truncating is the wrong remedy anyway, because the tail is not the least
important part of a note; it is merely the newest. What the budget actually
wants is a note shaped like an INDEX: a parent short enough to load whole, and
the detail one hop away in notes the assistant opens when it needs them. That is
the shape this module produces, along one boundary the author already drew —
the `##` sections.

Two invariants, both tested by planting their violation:

* **No content is lost.** Every non-blank line of the source ends up in the
  parent or in exactly one child. A splitter that drops the tail would have
  reproduced the bug it exists to fix.
* **The graph stays connected.** The parent links every child, each child links
  back. An orphaned child is worse than an over-budget note: it is knowledge
  that no longer has a way in.

Planning and writing are separate on purpose: `plan()` is pure, so `--dry-run`
shows exactly what `mb note split` would do, and the CLI is the only thing that
touches the filesystem.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, NamedTuple

from ..search.chunking import parse_frontmatter
from .capture import safe_stem

_SECTION_RE = re.compile(r"^## +(.+?)\s*$", re.MULTILINE)
INDEX_HEADING = "Разделы"
# Frontmatter keys a child inherits. `created` is re-stamped (the child is new),
# `status`/`owner`/`source` are the parent's editorial state and would be a
# claim the child cannot back up.
_INHERITED = ("type", "tags", "project")


class Child(NamedTuple):
    stem: str
    rel_path: str
    heading: str
    text: str
    summary: str


class SplitPlan(NamedTuple):
    parent_rel: str
    parent_text: str
    children: list[Child]
    source_body_lines: int
    parent_body_lines: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _sections(body: str) -> tuple[str, list[tuple[str, str]]]:
    """(intro, [(heading, content), ...]) split on top-level `##`."""
    marks = list(_SECTION_RE.finditer(body))
    if not marks:
        return body, []
    intro = body[: marks[0].start()]
    out: list[tuple[str, str]] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        out.append((m.group(1).strip(), body[m.end(): end].strip("\n")))
    return intro, out


def _summary_of(content: str, limit: int = 90) -> str:
    """First non-blank, non-heading line, trimmed. The index line has to say
    what is behind the link — a bare list of titles makes the reader open all
    of them, which is the cost the split was meant to remove."""
    for line in content.splitlines():
        s = line.strip().lstrip("#").strip()
        if not s or s.startswith(("|", "```", "<!--")):
            continue
        s = re.sub(r"[*_`\[\]]", "", s)
        return s[: limit - 1] + "…" if len(s) > limit else s
    return ""


def _render_frontmatter(fm: dict[str, Any], title: str, created: str,
                        related: list[str]) -> str:
    lines = ["---", f'title: "{title}"']
    for key in _INHERITED:
        val = fm.get(key)
        if val in (None, "", []):
            continue
        if isinstance(val, list):
            lines.append(f"{key}: [{', '.join(str(v) for v in val)}]")
        else:
            lines.append(f"{key}: {val}")
    lines.append(f"created: {created}")
    lines.append("related: [" + ", ".join(f'"{r}"' for r in related) + "]")
    lines.append("---")
    return "\n".join(lines)


def plan(raw: str, rel_path: str, created: str,
         max_lines: int | None = None) -> SplitPlan:
    """Compute the split without touching disk.

    `max_lines` is advisory: it is reported against the resulting parent so the
    caller can say whether the split actually solved the budget, but a note is
    never refused for being small — splitting a note by hand is a legitimate
    thing to want.
    """
    fm, body = parse_frontmatter(raw)
    body = body.strip("\n")
    source_lines = len([ln for ln in body.splitlines() if ln.strip()])
    parent_stem = Path(rel_path).stem
    folder = str(Path(rel_path).parent).replace("\\", "/")
    parent_title = str(fm.get("title") or parent_stem)

    intro, sections = _sections(body)
    if len(sections) < 2:
        return SplitPlan(rel_path, raw, [], source_lines, source_lines,
                         error=f"{rel_path} has {len(sections)} `##` section(s) — "
                               "nothing to split along. Add section headings first, "
                               "or shorten the note by hand.")

    seen: set[str] = set()
    children: list[Child] = []
    for heading, content in sections:
        stem = safe_stem(f"{parent_stem} - {heading}")
        if not stem:
            stem = safe_stem(f"{parent_stem} - section {len(children) + 1}")
        # Two sections can legitimately carry the same heading; two files cannot.
        base, n = stem, 2
        while stem in seen:
            stem, n = f"{base} ({n})", n + 1
        seen.add(stem)

        child_fm = _render_frontmatter(fm, f"{parent_title} — {heading}", created,
                                       [f"[[{parent_stem}]]"])
        text = f"{child_fm}\n\n# {parent_title} — {heading}\n\n{content}\n"
        children.append(Child(stem, f"{folder}/{stem}.md" if folder != "." else f"{stem}.md",
                              heading, text, _summary_of(content)))

    index_lines = [f"## {INDEX_HEADING}", ""]
    for c in children:
        tail = f" — {c.summary}" if c.summary else ""
        index_lines.append(f"- [[{c.stem}]]{tail}")

    fm_block = raw[: raw.index(body)].rstrip("\n") if body and body in raw else ""
    parent_body = intro.strip("\n") + "\n\n" + "\n".join(index_lines) + "\n"
    parent_text = (f"{fm_block}\n\n{parent_body}" if fm_block else parent_body)
    parent_lines = len([ln for ln in parent_body.splitlines() if ln.strip()])

    return SplitPlan(rel_path, parent_text, children, source_lines, parent_lines)


def content_is_preserved(raw: str, plan_: SplitPlan) -> bool:
    """Every non-blank source body line survives in the parent or a child.

    Section headings are excluded: they become child titles and index entries,
    so they change shape by design. Everything else must be findable verbatim.
    """
    _, body = parse_frontmatter(raw)
    haystack = plan_.parent_text + "\n" + "\n".join(c.text for c in plan_.children)
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("## "):
            continue
        if s not in haystack:
            return False
    return True
