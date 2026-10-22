"""The `ticket` note type: a unit of work that lives in the vault.

A ticket is an ordinary vault note (`type: ticket`, so `mb search --type
ticket` finds it) in `tickets/<project>/<ID> - <slug>.md`. The engine owns its
shape: the frontmatter schema and the body sections are generated here, because
there is no template in the vault and a ticket needs a guaranteed structure.

Public API (the contract `mb board` builds on; every function is pure or
touches only the paths it is given):

    TicketError                         raised for every refusal (message is user-facing)
    KINDS, STATUSES, PRIORITIES         allowed values; first-listed default is DEFAULT_*
    SECTION_GOAL / _CRITERIA / _FEED / _VERDICT   body section names (## headings)

    prefix_for(registry, project)       -> str | None   the project's ticket prefix
    require_prefix(registry, project)   -> str          same, TicketError when absent
    ticket_dir(vault, project)          -> Path         tickets/<project>
    next_id(vault, project, prefix)     -> str          "<PREFIX>-<max existing + 1>"
    render_ticket(...)                  -> str          full note text (keyword-only)
    parse_ticket(path)                  -> dict         {"path", "frontmatter", "sections"}
    write_ticket(path, text)            -> None         atomic (tmp + os.replace), refuses overwrite
    find_ticket(vault, ticket_id)       -> Path | None  the file of an id, in any project
    iter_tickets(vault, project=None)   -> list[dict]   parse_ticket of every ticket, sorted by id
    update_ticket(path, *, fields=None, feed=None, by=None, check=None) -> dict
                                        locked read-modify-write of ONE ticket: sets
                                        frontmatter keys, appends a dated line to ## Feed,
                                        refreshes `updated`; other sections stay byte-identical
    replace_section(text, name, body)   -> str          swap one "## name" body, the rest untouched
    create_ticket(vault, registry, project, title, ...) -> Path
                                        the whole creation under the per-project lock:
                                        validate, allocate the ID, render, write

`parse_ticket` returns `frontmatter` with plain Python values (dates as
`YYYY-MM-DD` strings, lists as lists, absent `parent`/`assignee`/`agent` as
None) and `sections` as {section name: stripped text}, always holding all four
section names. `render_ticket` then `parse_ticket` is lossless for every field.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..platform.fslock import break_stale_lock, make_lock_dir
from . import capture

TICKETS_DIR = "tickets"

KINDS = ("epic", "feature", "task", "bug", "spike", "review", "chore")
STATUSES = ("backlog", "ready", "in-progress", "review", "done", "blocked")
PRIORITIES = ("P0", "P1", "P2", "P3")
DEFAULT_KIND = "task"
DEFAULT_STATUS = "backlog"
DEFAULT_PRIORITY = "P2"

SECTION_GOAL = "Goal"
SECTION_CRITERIA = "Acceptance criteria"
SECTION_FEED = "Feed"
SECTION_VERDICT = "Verdict"
SECTIONS = (SECTION_GOAL, SECTION_CRITERIA, SECTION_FEED, SECTION_VERDICT)

PREFIX_RE = re.compile(r"^[A-Z]{2,6}$")
_ID_RE = re.compile(r"^([A-Z]{2,6})-(\d+)$")

_LOCK_STALE_SECONDS = 30.0
_LOCK_WAIT_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.02


class TicketError(Exception):
    """A refusal with a message fit to print. Raised before anything is written."""


def prefix_for(registry: dict[str, Any] | None, project: str | None) -> str | None:
    """The project's `ticket_prefix`, or None (no project, unregistered, no prefix)."""
    if not project:
        return None
    entry = (registry or {}).get(project)
    prefix = entry.get("ticket_prefix") if isinstance(entry, dict) else None
    return prefix if isinstance(prefix, str) and PREFIX_RE.match(prefix) else None


def require_prefix(registry: dict[str, Any] | None, project: str | None) -> str:
    prefix = prefix_for(registry, project)
    if prefix is None:
        if not project:
            raise TicketError(
                "a ticket belongs to a registered project: pass --project <id> "
                "(general and personal notes cannot hold tickets)")
        raise TicketError(
            f"project '{project}' has no ticket_prefix: set one "
            f"(2-6 capital letters) with `mb project set "
            f"{project} --ticket-prefix <PREFIX>`")
    return prefix


def ticket_dir(vault: Path, project: str) -> Path:
    return Path(vault) / TICKETS_DIR / project


def next_id(vault: Path, project: str, prefix: str) -> str:
    """`<prefix>-<n>` with n = highest number among the project's existing
    tickets + 1 (a deleted or skipped number is never reused or counted).
    Not safe against a concurrent writer on its own: `create_ticket` holds the
    per-project lock around this call."""
    highest = 0
    folder = ticket_dir(vault, project)
    if folder.is_dir():
        pat = re.compile(rf"^{re.escape(prefix)}-(\d+)(?: - .*)?\.md$")
        for f in folder.iterdir():
            m = pat.match(f.name)
            if m:
                highest = max(highest, int(m.group(1)))
    return f"{prefix}-{highest + 1}"


def _check_choice(name: str, value: str, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        raise TicketError(f"{name} '{value}' is not one of: {', '.join(allowed)}")


def _check_ids(name: str, values: list[str]) -> None:
    for v in values:
        if not _ID_RE.match(v):
            raise TicketError(f"{name} '{v}' is not a ticket id like DD-12")


def _scalar(value: Any) -> str:
    return "null" if value is None else json.dumps(value, ensure_ascii=False)


def _flow(values: list[str]) -> str:
    return "[" + ", ".join(json.dumps(v, ensure_ascii=False) for v in values) + "]"


def render_ticket(*, id: str, title: str, project: str, kind: str = DEFAULT_KIND,
                  status: str = DEFAULT_STATUS, priority: str = DEFAULT_PRIORITY,
                  parent: str | None = None, blocked_by: list[str] | None = None,
                  owns: list[str] | None = None, contracts: list[str] | None = None,
                  assignee: str | None = None, agent: str | None = None,
                  created: str | None = None, updated: str | None = None,
                  goal: str = "", criteria: str = "") -> str:
    """The full note text. Values are validated here, so no invalid ticket is
    ever rendered. Frontmatter strings are JSON-quoted (valid YAML)."""
    if not _ID_RE.match(id):
        raise TicketError(f"id '{id}' is not a ticket id like DD-12")
    _check_choice("kind", kind, KINDS)
    _check_choice("status", status, STATUSES)
    _check_choice("priority", priority, PRIORITIES)
    blocked_by, owns, contracts = list(blocked_by or []), list(owns or []), list(contracts or [])
    _check_ids("blocked_by", blocked_by)
    if parent is not None:
        _check_ids("parent", [parent])
    today = datetime.now().strftime("%Y-%m-%d")
    created = created or today
    updated = updated or created
    head = [
        "---",
        "type: ticket",
        f"title: {_scalar(f'{id} {title}')}",
        f"aliases: {_flow([id])}",
        f"id: {id}",
        f"project: {project}",
        f"kind: {kind}",
        f"status: {status}",
        f"priority: {priority}",
        f"parent: {_scalar(parent)}",
        f"blocked_by: {_flow(blocked_by)}",
        f"owns: {_flow(owns)}",
        f"contracts: {_flow(contracts)}",
        f"assignee: {_scalar(assignee)}",
        f"agent: {_scalar(agent)}",
        f"created: {created}",
        f"updated: {updated}",
        "---",
        "",
        f"# {id} {title}",
        "",
    ]
    parts = {SECTION_GOAL: goal, SECTION_CRITERIA: criteria, SECTION_FEED: "", SECTION_VERDICT: ""}
    body: list[str] = []
    for name in SECTIONS:
        text = parts[name].strip()
        body += [f"## {name}", "", *([text, ""] if text else [""])]
    return "\n".join(head + body)


def _coerce(value: Any) -> Any:
    """Normalise what YAML (or the engine's fallback line parser, which leaves
    JSON-looking text as strings) hands back."""
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, str):
        v = value.strip()
        if v in ("null", "~", ""):
            return None
        if v[:1] in ('[', '"'):
            try:
                return _coerce(json.loads(v))
            except ValueError:
                return value
    if isinstance(value, list):
        return [str(x) for x in value]
    return value


def parse_ticket(path: Path) -> dict[str, Any]:
    """Read a ticket: {"path": Path, "frontmatter": {...}, "sections": {...}}.
    Raises TicketError if the file is not a ticket note."""
    from ..search.chunking import parse_frontmatter

    path = Path(path)
    raw, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    fm = {k: _coerce(v) for k, v in raw.items()}
    if fm.get("type") != "ticket":
        raise TicketError(f"{path}: not a ticket note (type: {fm.get('type')!r})")
    for key in ("blocked_by", "owns", "contracts", "aliases"):
        if fm.get(key) is None:
            fm[key] = []
    sections = dict.fromkeys(SECTIONS, "")
    current: str | None = None
    buf: list[str] = []
    for line in body.splitlines():
        m = re.match(r"^## (.+?)\s*$", line)
        if m and m.group(1) in sections:
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current, buf = m.group(1), []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return {"path": path, "frontmatter": fm, "sections": sections}


def write_ticket(path: Path, text: str) -> None:
    """Write atomically (tmp in the same directory + `os.replace`): a reader
    never sees a half-written ticket and a crash leaves no partial file.
    Refuses to overwrite: a ticket id is never reused."""
    path = Path(path)
    if path.exists():
        raise TicketError(f"already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _acquire(lock: Path) -> None:
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    while not make_lock_dir(lock):
        if break_stale_lock(lock, _LOCK_STALE_SECONDS):      # a holder that died; a live one finishes in milliseconds
            continue
        if time.monotonic() >= deadline:
            raise TicketError(f"another process holds the ticket lock ({lock})")
        time.sleep(_LOCK_POLL_SECONDS)


def create_ticket(vault: Path, registry: dict[str, Any] | None, project: str | None,
                  title: str, *, kind: str = DEFAULT_KIND, status: str = DEFAULT_STATUS,
                  priority: str = DEFAULT_PRIORITY, parent: str | None = None,
                  blocked_by: list[str] | None = None, owns: list[str] | None = None,
                  contracts: list[str] | None = None, assignee: str | None = None,
                  agent: str | None = None, goal: str = "", criteria: str = "") -> Path:
    """Validate, allocate the next ID under the per-project lock, write. Returns
    the new path. Every refusal (no project, no prefix, bad value, unusable
    title) is a TicketError raised BEFORE anything is written."""
    prefix = require_prefix(registry, project)
    assert project is not None
    slug = capture.safe_stem(title)
    if not slug:
        raise TicketError(f"{title!r} leaves nothing usable as a filename")
    vault = Path(vault)
    lock = vault / ".mb" / f"tickets-{project}.lock"
    _acquire(lock)
    try:
        tid = next_id(vault, project, prefix)
        known = {str(t["frontmatter"]["id"]) for t in iter_tickets(vault)}
        for field, refs in (("parent", [parent] if parent else []), ("blocked_by", blocked_by or [])):
            for ref in refs:
                if ref not in known:
                    raise TicketError(f"{field} {ref} is not a ticket (nothing was written)")
        text = render_ticket(id=tid, title=title, project=project, kind=kind,
                             status=status, priority=priority, parent=parent,
                             blocked_by=blocked_by, owns=owns, contracts=contracts,
                             assignee=assignee, agent=agent, goal=goal, criteria=criteria)
        path = ticket_dir(vault, project) / f"{tid} - {slug}.md"
        write_ticket(path, text)
        return path
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass


@contextlib.contextmanager
def _ticket_lock(vault: Path, ticket_id: str):
    lock = Path(vault) / ".mb" / f"ticket-{ticket_id}.lock"
    _acquire(lock)
    try:
        yield
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass


def find_ticket(vault: Path, ticket_id: str) -> Path | None:
    """The ticket's file, or None. The id must be shaped like DD-12 BEFORE it
    reaches the glob: `DEMO-?` or `DEMO-[3]` would otherwise match other tickets."""
    root = Path(vault) / TICKETS_DIR
    if not _ID_RE.fullmatch(ticket_id) or not root.is_dir():
        return None
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        for f in sorted(folder.glob(f"{ticket_id} - *.md")) + sorted(folder.glob(f"{ticket_id}.md")):
            return f
    return None


def _id_key(ticket_id: str) -> tuple[str, int]:
    m = _ID_RE.match(ticket_id)
    return (m.group(1), int(m.group(2))) if m else (ticket_id, 0)


def iter_tickets(vault: Path, project: str | None = None) -> list[dict[str, Any]]:
    """Every parsable ticket (a file that is not a ticket is skipped), sorted
    by (prefix, number). Each dict is `parse_ticket`'s, with `frontmatter["project"]`
    falling back to the folder name."""
    root = Path(vault) / TICKETS_DIR
    out: list[dict[str, Any]] = []
    if not root.is_dir():
        return out
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        if project and folder.name != project:
            continue
        for f in sorted(folder.glob("*.md")):
            try:
                tk = parse_ticket(f)
            except (TicketError, OSError):
                continue
            tk["frontmatter"].setdefault("project", folder.name)
            if not tk["frontmatter"].get("id"):
                continue
            out.append(tk)
    out.sort(key=lambda tk: _id_key(str(tk["frontmatter"]["id"])))
    return out


def _fm_value(key: str, value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return _flow([str(v) for v in value])
    if key in ("created", "updated") and value is not None:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]+", value) and key in (
            "status", "priority", "kind", "id", "project"):
        return value
    return _scalar(value)


def _set_frontmatter(text: str, fields: dict[str, Any]) -> str:
    end = text.find("\n---", 3)
    if not text.startswith("---") or end == -1:
        raise TicketError("not a ticket: no frontmatter")
    head, rest = text[:end], text[end:]
    for key, value in fields.items():
        line = f"{key}: {_fm_value(key, value)}"
        pat = re.compile(rf"^{re.escape(key)}:.*$", re.M)
        if pat.search(head):
            head = pat.sub(lambda _m, ln=line: ln, head, count=1)
        else:
            head += f"\n{line}"
    return head + rest


def _append_feed(text: str, line: str) -> str:
    m = re.search(rf"^## {re.escape(SECTION_FEED)}[ \t]*$", text, re.M)
    if not m:
        raise TicketError(f"ticket has no '## {SECTION_FEED}' section")
    nxt = re.search(r"^## ", text[m.end():], re.M)
    stop = m.end() + nxt.start() if nxt else len(text)
    section = text[m.end():stop].rstrip("\n")
    tail = "\n\n" if nxt else "\n"
    return text[:m.end()] + section + "\n" + line + tail + text[stop:]


def replace_section(text: str, name: str, body: str) -> str:
    """Replace the body of the `## name` section; every other byte is kept."""
    m = re.search(rf"^## {re.escape(name)}[ \t]*$", text, re.M)
    if not m:
        raise TicketError(f"ticket has no '## {name}' section")
    nxt = re.search(r"^## ", text[m.end():], re.M)
    stop = m.end() + nxt.start() if nxt else len(text)
    body = body.strip()
    middle = f"\n\n{body}\n" + ("\n" if nxt else "") if body else "\n\n" + ("\n" if nxt else "")
    return text[:m.end()] + middle + text[stop:]


def update_ticket(path: Path, *, fields: Any = None,
                  feed: Any = None, by: str | None = None,
                  check: Any = None, rewrite: Any = None) -> dict[str, Any]:
    """Locked, atomic read-modify-write of one ticket; returns `parse_ticket`
    of the result. `fields` (dict, or a callable of the parsed ticket returning one) replaces
    or adds frontmatter keys; `feed` (str or callable of the parsed ticket) appends
    `- YYYY-MM-DD HH:MM <by>: <feed>` to `## Feed`; `updated` is refreshed.
    `rewrite(text, parsed)` returns the new full text (section and heading edits,
    applied after the frontmatter keys, before the feed line).
    `check(parsed)` runs under the lock on the CURRENT content and may raise
    (any exception) to abort with the file untouched. Two processes calling this on
    one ticket never lose each other's lines."""
    path = Path(path)
    vault = path.parents[2]
    tid = path.name.split(" - ", 1)[0].removesuffix(".md")
    with _ticket_lock(vault, tid):
        parsed = parse_ticket(path)
        if check is not None:
            check(parsed)
        text = path.read_text(encoding="utf-8")
        now = datetime.now()
        new_fields = dict(fields(parsed) if callable(fields) else (fields or {}))
        new_fields["updated"] = now.strftime("%Y-%m-%d")
        text = _set_frontmatter(text, new_fields)
        if rewrite is not None:
            text = rewrite(text, parsed)
        feed = feed(parsed) if callable(feed) else feed
        if feed:
            one = " ".join(feed.split())
            text = _append_feed(text, f"- {now.strftime('%Y-%m-%d %H:%M')} {by or 'lead'}: {one}")
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()
    return parse_ticket(path)
