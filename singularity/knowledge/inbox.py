"""A staging area for captures that are not yet knowledge (AUD-21).

`mb learn` and `mb note new` both demand a decision at the worst moment: which
type is this, where does it belong, does it already exist? Under pressure the
honest answer is often "I don't know yet, but I must not lose it", and the two
bad outcomes follow from having nowhere to put that — the thought is dropped,
or it lands in a managed root as a note nobody can classify later.

The inbox is that nowhere-to-put-it place, with one rule that makes it safe:
**nothing leaves it without a human saying so.** This module SUGGESTS a type,
existing notes that look like duplicates, and links; it never files, merges or
deletes. `approve()` is the only function that moves anything, and it moves
exactly where the caller says.

Opt-in (`inbox_enabled`): a second place where knowledge can sit is a cost, and
a user who is happy deciding at capture time should not pay it.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import INBOX_DIR, Config
DUP_THRESHOLD = 0.75          # cosine above which a hit is offered as "already have this"
SUGGEST_TOP_K = 5

# type -> managed root, mirroring `mb note new` so an approved capture lands
# exactly where the same note created by hand would have.
TYPE_ROOTS = {
    "learning": "learnings/active",
    "decision": "decisions",
    "howto": "howto",
    "incident": "incidents",
    "rule": "rules",
    "reference": "reference",
    "changelog": "changelog",
    "moc": "_moc",
    "daily": "daily",
}

_SLUG_RE = re.compile(r"[^\w\s.-]", re.UNICODE)


def enabled(cfg: Config) -> bool:
    return bool(cfg.get("inbox_enabled"))


def inbox_dir(cfg: Config) -> Path:
    return cfg.resolve(INBOX_DIR)


def capture(cfg: Config, text: str, title: str | None = None,
            now: str | None = None) -> dict[str, Any]:
    """Write a capture down verbatim. No type, no classification, no index.

    The frontmatter is deliberately minimal: `type: inbox` is not one of the
    managed types, so a capture cannot be mistaken for a filed note by the
    curator, the digest planner or a search filter.
    """
    stamp = now or datetime.now().isoformat(timespec="seconds")
    first_line = next((ln.strip(" #") for ln in text.splitlines() if ln.strip()), "capture")
    name = _slug(title or first_line)[:60] or "capture"
    directory = inbox_dir(cfg)
    directory.mkdir(parents=True, exist_ok=True)
    path = _unique(directory / f"{name}.md")
    path.write_text(
        f"---\ntype: inbox\ntitle: \"{title or first_line}\"\ncaptured: {stamp}\n---\n\n"
        f"{text.rstrip()}\n", encoding="utf-8")
    return {"file": f"{INBOX_DIR}/{path.name}", "title": title or first_line,
            "captured": stamp}


def items(cfg: Config) -> list[dict[str, Any]]:
    """Everything waiting, oldest first — the order a person works through."""
    directory = inbox_dir(cfg)
    if not directory.exists():
        return []
    out = []
    for path in sorted(directory.glob("*.md"), key=lambda p: p.stat().st_mtime):
        text = path.read_text(encoding="utf-8", errors="replace")
        out.append({"file": f"{INBOX_DIR}/{path.name}",
                    "title": _frontmatter_title(text) or path.stem,
                    "chars": len(text)})
    return out


def suggest(cfg: Config, rel: str, search=None) -> dict[str, Any]:
    """What this capture might be — offered, never applied.

    `search` is injected so the caller decides whether to pay for a daemon
    round-trip; without it the suggestion is still useful (type guess from the
    text), just without duplicate detection.
    """
    path = cfg.resolve(rel)
    if not path.exists():
        return {"error": f"not in the inbox: {rel}"}
    text = path.read_text(encoding="utf-8", errors="replace")
    body = _body(text)
    result: dict[str, Any] = {
        "file": rel,
        "title": _frontmatter_title(text) or path.stem,
        "suggested_type": guess_type(body),
        "duplicates": [],
        "links": [],
    }
    if search is None:
        return result
    hits = (search({"query": body[:400], "top_k": SUGGEST_TOP_K,
                    "memory_only": True, "format": "json"}) or {}).get("results", [])
    for hit in hits:
        meta, score = hit.get("meta", {}), float(hit.get("score", 0))
        entry = {"file": meta.get("file"), "score": round(score, 3),
                 "title": meta.get("title")}
        if score >= DUP_THRESHOLD:
            result["duplicates"].append(entry)
        else:
            result["links"].append(entry)
    return result


def approve(cfg: Config, rel: str, note_type: str, title: str | None = None) -> dict[str, Any]:
    """Move a capture into its managed root. The ONLY function here that files.

    Refuses rather than guesses: an unknown type, a missing capture or an
    occupied destination all come back as an error, because the alternative is
    a note quietly landing somewhere its author did not choose.
    """
    if note_type not in TYPE_ROOTS:
        return {"error": f"unknown type {note_type!r} (known: {', '.join(sorted(TYPE_ROOTS))})"}
    src = cfg.resolve(rel)
    if not src.exists():
        return {"error": f"not in the inbox: {rel}"}
    text = src.read_text(encoding="utf-8", errors="replace")
    name = _slug(title or _frontmatter_title(text) or src.stem)[:60] or src.stem
    dest_dir = cfg.memory_folder(TYPE_ROOTS[note_type])
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{name}.md"
    if dest.exists():
        return {"error": f"already exists: {TYPE_ROOTS[note_type]}/{dest.name}"}
    dest.write_text(_retype(text, note_type, title), encoding="utf-8")
    src.unlink()
    return {"from": rel, "file": f"{TYPE_ROOTS[note_type]}/{dest.name}", "type": note_type}


# --- helpers ----------------------------------------------------------------

#: English keywords, and the engine says so rather than pretending otherwise:
#: a note written in another language falls through to the default, which is
#: the same outcome as an unrecognised English one. The cost of a miss here is
#: one extra keystroke at `mb inbox approve`, never a wrong file on disk.
_TYPE_HINTS = (
    ("decision", ("decided", "decision", "trade-off", "we chose", "owner decision")),
    ("incident", ("broke", "incident", "crashed", "regression", "traceback", "error")),
    ("howto", ("step ", "how to", "howto", "instructions", "run ")),
    ("rule", ("always", "never", "must ", "rule")),
)


def guess_type(body: str) -> str:
    """A hint, not a verdict — the caller still has to say yes.

    Deliberately keyword-based rather than semantic: a wrong guess dressed up
    as a model's opinion is harder to overrule than an obviously shallow one.
    """
    low = body.lower()
    for note_type, hints in _TYPE_HINTS:
        if any(h in low for h in hints):
            return note_type
    return "learning"


def _retype(text: str, note_type: str, title: str | None) -> str:
    """Rewrite the capture's frontmatter for its new home, keeping the body and
    the original capture timestamp (provenance: when it was thought, not when
    it was filed)."""
    body = _body(text)
    captured = _frontmatter_field(text, "captured") or ""
    kept_title = title or _frontmatter_title(text) or "Untitled"
    lines = [f"type: {note_type}", f'title: "{kept_title}"', "tags: []",
             f"created: {datetime.now().date().isoformat()}"]
    if captured:
        lines.append(f"captured: {captured}")
    return "---\n" + "\n".join(lines) + "\n---\n\n" + body.strip() + "\n"


def _body(text: str) -> str:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2].lstrip("\n")
    return text


def _frontmatter_field(text: str, field: str) -> str | None:
    match = re.search(rf"^{field}:\s*(.+)$", text[:600], re.M)
    return match.group(1).strip().strip('"') if match else None


def _frontmatter_title(text: str) -> str | None:
    return _frontmatter_field(text, "title")


def _slug(value: str) -> str:
    return _SLUG_RE.sub("", value).strip().replace("/", "-")


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 2
    while (candidate := path.with_name(f"{stem}-{n}{suffix}")).exists():
        n += 1
    return candidate
