"""The board: a view over tickets plus the rules for moving them.

Pure logic over `knowledge/tickets.py`; the CLI (`commands/board.py`) only
prints. Every rule that can refuse a move lives here so `mb board move` and
`mb board check` cannot disagree.

Refusal codes (stable, they appear in `--json`):

    bad_transition       the status graph has no such edge           (never bypassed)
    done_requires_review `done` is reachable only from `review`      (never bypassed)
    no_owns              in-progress without `owns` on a file-changing kind (force-able)
    blocked_cycle        `blocked_by` loops back to the ticket          (never bypassed)
    no_verdict           moving to `done` with an empty Verdict       (force-able)
    unknown_ref          `blocked_by` / `parent` names no ticket      (never bypassed)
    blocked_open         a `blocked_by` ticket is not done            (force-able)
    no_criteria          empty "Acceptance criteria"                  (force-able)
    wip_limit            too many tickets in-progress in the project  (force-able)
    owns_overlap         `owns` collides with an in-progress ticket   (force-able)

Public API:

    Violation(code, message)
    can_force(violations)                  -> bool
    TRANSITIONS                            status -> allowed next statuses
    columns(tickets)                       -> {status: [ticket, ...]} for every status
    next_ready(tickets)                    -> ready tickets with all blockers done, best first
    criteria_filled(text)                  -> bool
    owns_collide(a, b)                     -> bool
    wip_limit(cfg_get, project)            -> int
    check_move(tickets, ticket, target, limit)     -> [Violation]
    check_all(tickets, limit_for)          -> [dict]   invariants over the whole board
    move(vault, cfg_get, ticket_id, target, force_reason=None, by=None) -> dict
    add_note(vault, ticket_id, text, by=None)      -> dict
    assign(vault, ticket_id, assignee=None, agent=None, by=None) -> dict
    find_cycle(blocked)                 -> list[str] | None   a blocked_by cycle (incl. self-block)
    edit(vault, ticket_id, changes, by=None) -> dict
        `changes` keys: parent, goal, criteria (list, replaces), add_criteria (list), verdict,
        title, priority, kind, blocked_by / remove_blocked_by, owns / remove_owns.
        id, status and project are refused (status moves only through `move`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import tickets as tk

DEFAULT_WIP_LIMIT = 4
# kinds whose work changes files: without `owns` they would collide invisibly
OWNS_KINDS = ("feature", "task", "bug", "chore")
_CRITERIA_FROM = ("ready", "in-progress", "review", "done")
_PLACEHOLDERS = {"tbd", "todo", "-", "- [ ]", "*", "n/a", "..."}
_FORCEABLE = ("blocked_open", "no_criteria", "wip_limit", "owns_overlap", "no_verdict", "no_owns")

TRANSITIONS: dict[str, tuple[str, ...]] = {
    "backlog": ("ready", "blocked"),
    "ready": ("in-progress", "backlog", "blocked"),
    "in-progress": ("review", "blocked"),
    "review": ("done", "in-progress", "blocked"),
    "blocked": (),          # only back to `blocked_from`, see _allowed
    "done": (),
}


@dataclass(frozen=True)
class Violation:
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def can_force(violations: list[Violation]) -> bool:
    return all(v.code in _FORCEABLE for v in violations)


def _fm(ticket: dict[str, Any]) -> dict[str, Any]:
    return ticket["frontmatter"]


def _id(ticket: dict[str, Any]) -> str:
    return str(_fm(ticket)["id"])


def columns(tickets: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    cols: dict[str, list[dict[str, Any]]] = {s: [] for s in tk.STATUSES}
    for t in tickets:
        cols.setdefault(str(_fm(t).get("status")), []).append(t)
    return cols


def _by_id(tickets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {_id(t): t for t in tickets}


def _open_blockers(t: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> list[str]:
    return [b for b in _fm(t).get("blocked_by") or []
            if b not in by_id or _fm(by_id[b]).get("status") != "done"]


def _unknown_refs(t: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> list[str]:
    refs = list(_fm(t).get("blocked_by") or [])
    if _fm(t).get("parent"):
        refs.append(str(_fm(t)["parent"]))
    return [r for r in refs if r not in by_id]


_PRIORITY_RANK = {p: i for i, p in enumerate(tk.PRIORITIES)}


def next_ready(tickets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Non-epic `ready` tickets whose blockers are all `done` (an unknown blocker counts
    as open), ordered P0 to P3 and then by id."""
    by_id = _by_id(tickets)
    ready = [t for t in tickets if _fm(t).get("status") == "ready"
             and not _is_epic(t) and not _open_blockers(t, by_id)]
    return sorted(ready, key=lambda t: (_PRIORITY_RANK.get(str(_fm(t).get("priority")), 99),
                                        tk._id_key(_id(t))))


def criteria_filled(text: str) -> bool:
    """False for empty text, an HTML comment only, or placeholder lines only."""
    stripped = re.sub(r"<!--.*?-->", "", text or "", flags=re.S)
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    return any(ln.lower() not in _PLACEHOLDERS for ln in lines)


def _norm_path(p: str) -> str:
    p = p.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.rstrip("/")


def owns_collide(a: list[str], b: list[str]) -> list[tuple[str, str]]:
    """Pairs (x, y) with x from a and y from b that are equal or one is a
    path-segment prefix of the other (`src/a` and `src/a/b.py`, not `src/ab`)."""
    hits = []
    for x in map(_norm_path, a):
        for y in map(_norm_path, b):
            if x and y and (x == y or x.startswith(y + "/") or y.startswith(x + "/")):
                hits.append((x, y))
    return hits


def wip_limit(cfg_get: Any, project: str) -> int:
    entry = (cfg_get("projects", {}) or {}).get(project)
    per = entry.get("wip_limit") if isinstance(entry, dict) else None
    if isinstance(per, int) and not isinstance(per, bool) and per >= 1:
        return per
    base = cfg_get("board_wip_limit", DEFAULT_WIP_LIMIT)
    return base if isinstance(base, int) and not isinstance(base, bool) and base >= 1 \
        else DEFAULT_WIP_LIMIT


def _is_epic(t: dict[str, Any]) -> bool:
    return _fm(t).get("kind") == "epic"


def _in_progress(tickets: list[dict[str, Any]], project: str,
                 skip: str | None = None) -> list[dict[str, Any]]:
    return [t for t in tickets if _fm(t).get("project") == project
            and _fm(t).get("status") == "in-progress" and not _is_epic(t)
            and _id(t) != skip]


def _allowed(ticket: dict[str, Any], target: str) -> bool:
    fm = _fm(ticket)
    current = str(fm.get("status"))
    if current == "blocked":
        return target == (fm.get("blocked_from") or "backlog")
    return target in TRANSITIONS.get(current, ())


def check_move(tickets: list[dict[str, Any]], ticket: dict[str, Any],
               target: str, limit: int) -> list[Violation]:
    """Every reason `ticket` may not go to `target`; empty means allowed."""
    fm, tid, by_id = _fm(ticket), _id(ticket), _by_id(tickets)
    current = str(fm.get("status"))
    out: list[Violation] = []
    if target not in tk.STATUSES:
        return [Violation("bad_transition", f"unknown status '{target}'")]
    if target == "done" and current != "review":
        out.append(Violation("done_requires_review",
                             f"{tid} is '{current}': done is reachable only from review"))
    elif target == "done" and not ticket["sections"][tk.SECTION_VERDICT].strip():
        out.append(Violation("no_verdict", f"{tid} has an empty Verdict: write it first "
                                           f"(mb board edit {tid} --verdict ...)"))
    elif not _allowed(ticket, target):
        out.append(Violation("bad_transition", f"{tid}: {current} -> {target} is not an allowed move"))
    for ref in _unknown_refs(ticket, by_id):
        out.append(Violation("unknown_ref", f"{tid} refers to {ref}, which is not a ticket"))
    if target in _CRITERIA_FROM and not criteria_filled(ticket["sections"][tk.SECTION_CRITERIA]):
        out.append(Violation("no_criteria", f"{tid} has no acceptance criteria"))
    if target == "in-progress":
        open_ = _open_blockers(ticket, by_id)
        if open_:
            out.append(Violation("blocked_open", f"{tid} is blocked by {', '.join(open_)} (not done)"))
        if not _is_epic(ticket):
            project = str(fm.get("project"))
            running = _in_progress(tickets, project, skip=tid)
            if len(running) >= limit:
                out.append(Violation("wip_limit", f"{project} already has {len(running)} in progress "
                                                  f"(limit {limit})"))
            mine = list(fm.get("owns") or [])
            if fm.get("kind") in OWNS_KINDS and not mine:
                out.append(Violation("no_owns", f"{tid} is a {fm.get('kind')} without `owns`: name the "
                                                f"files or areas it may change (mb board edit {tid} --owns ...)"))
            for other in running:
                clash = owns_collide(mine, list(_fm(other).get("owns") or []))
                if clash:
                    out.append(Violation("owns_overlap", f"{tid} owns {clash[0][0]}, which "
                                                         f"{_id(other)} (in progress) owns as {clash[0][1]}"))
    return out


def find_cycle(blocked: dict[str, list[str]]) -> list[str] | None:
    """A cycle in the `blocked_by` graph as [a, b, ..., a], or None. A ticket
    blocked by itself is the one-element cycle [a, a]."""
    state: dict[str, int] = {}

    def walk(node: str, trail: list[str]) -> list[str] | None:
        state[node] = 1
        for nxt in blocked.get(node, []):
            if state.get(nxt) == 1:
                return trail[trail.index(nxt):] + [nxt] if nxt in trail else [nxt, nxt]
            if nxt in blocked and state.get(nxt) is None:
                found = walk(nxt, trail + [nxt])
                if found:
                    return found
        state[node] = 2
        return None

    for start in sorted(blocked):
        if state.get(start) is None:
            found = walk(start, [start])
            if found:
                return found
    return None


def check_all(tickets: list[dict[str, Any]], limit_for: Any) -> list[dict[str, str]]:
    """Invariants of the whole board as [{"id", "code", "message"}]."""
    by_id, found = _by_id(tickets), []

    def add(t: dict[str, Any], v: Violation) -> None:
        found.append({"id": _id(t), **v.as_dict()})

    for t in tickets:
        fm, status = _fm(t), str(_fm(t).get("status"))
        if status not in tk.STATUSES:
            add(t, Violation("bad_transition", f"unknown status '{status}'"))
        for ref in _unknown_refs(t, by_id):
            add(t, Violation("unknown_ref", f"refers to {ref}, which is not a ticket"))
        if status in _CRITERIA_FROM and not criteria_filled(t["sections"][tk.SECTION_CRITERIA]):
            add(t, Violation("no_criteria", f"is '{status}' without acceptance criteria"))
        if status in ("in-progress", "review", "done"):
            open_ = _open_blockers(t, by_id)
            if open_ and status == "in-progress":
                add(t, Violation("blocked_open", f"is in progress while {', '.join(open_)} is not done"))
        if status == "done" and not t["sections"][tk.SECTION_VERDICT].strip():
            add(t, Violation("no_verdict", "is done with an empty Verdict"))
        if not t["path"].name.startswith(f"{fm.get('id')} - "):
            add(t, Violation("id_mismatch", f"file name {t['path'].name} does not start with its id"))
    cycle = find_cycle({_id(t): list(_fm(t).get("blocked_by") or []) for t in tickets})
    if cycle:
        add(by_id[cycle[0]], Violation("blocked_cycle", "blocked_by loops: " + " -> ".join(cycle)))
    for project in sorted({str(_fm(t).get("project")) for t in tickets}):
        running = _in_progress(tickets, project)
        limit = limit_for(project)
        if len(running) > limit:
            add(running[0], Violation("wip_limit", f"{project} has {len(running)} in progress "
                                                   f"(limit {limit})"))
        for a in running:
            if _fm(a).get("kind") in OWNS_KINDS and not _fm(a).get("owns"):
                add(a, Violation("no_owns", "is in progress without `owns`"))
        for i, a in enumerate(running):
            for b in running[i + 1:]:
                clash = owns_collide(list(_fm(a).get("owns") or []), list(_fm(b).get("owns") or []))
                if clash:
                    add(a, Violation("owns_overlap", f"owns {clash[0][0]}, also owned by in-progress "
                                                     f"{_id(b)} ({clash[0][1]})"))
    return found


class BoardError(Exception):
    """Unknown ticket or other usage error (not a rule refusal)."""


def _need(vault: Path, ticket_id: str) -> Path:
    if not tk._ID_RE.fullmatch(ticket_id):
        raise BoardError(f"'{ticket_id}' is not a ticket id like DD-12")
    path = tk.find_ticket(vault, ticket_id)
    if path is None:
        raise BoardError(f"no ticket {ticket_id}")
    return path


class Refused(Exception):
    def __init__(self, violations: list[Violation]):
        super().__init__("; ".join(v.message for v in violations))
        self.violations = violations


def move(vault: Path, cfg_get: Any, ticket_id: str, target: str,
         force_reason: str | None = None, by: str | None = None) -> dict[str, Any]:
    """Move a ticket; raise `Refused` (file untouched) when a rule says no.
    `force_reason` bypasses only the force-able codes and is written to the
    Feed. The rules are evaluated under the ticket's lock, on current content."""
    path = _need(vault, ticket_id)
    state: dict[str, Any] = {}

    def gate(parsed: dict[str, Any]) -> None:
        everyone = [parsed if _id(t) == ticket_id else t for t in tk.iter_tickets(vault)]
        project = str(_fm(parsed).get("project"))
        violations = check_move(everyone, parsed, target, wip_limit(cfg_get, project))
        if violations:
            if not (force_reason and can_force(violations)):
                raise Refused(violations)
            state["forced"] = violations
        state["from"] = str(_fm(parsed).get("status"))

    def fields(parsed: dict[str, Any]) -> dict[str, Any]:
        was = str(_fm(parsed).get("status"))
        out: dict[str, Any] = {"status": target}
        if target == "blocked":
            out["blocked_from"] = was
        elif was == "blocked":
            out["blocked_from"] = None
        return out

    def feed_for(parsed: dict[str, Any]) -> str:
        line = f"status {_fm(parsed).get('status')} -> {target}"
        return line + (f" [force: {force_reason}]" if force_reason else "")

    result = tk.update_ticket(path, fields=fields, feed=feed_for, by=by, check=gate)
    return {"id": ticket_id, "from": state["from"], "to": target,
            "forced": [v.code for v in state.get("forced", [])], "path": str(result["path"])}


def add_note(vault: Path, ticket_id: str, text: str, by: str | None = None) -> dict[str, Any]:
    if not text.strip():
        raise BoardError("empty note")
    path = _need(vault, ticket_id)
    tk.update_ticket(path, feed=text, by=by)
    return {"id": ticket_id, "path": str(path)}


def assign(vault: Path, ticket_id: str, assignee: str | None = None,
           agent: str | None = None, by: str | None = None) -> dict[str, Any]:
    if assignee is None and agent is None:
        raise BoardError("nothing to assign: give --assignee and/or --agent")
    path = _need(vault, ticket_id)
    fields: dict[str, Any] = {}
    parts = []
    if assignee is not None:
        fields["assignee"] = assignee
        parts.append(f"assignee={assignee}")
    if agent is not None:
        fields["agent"] = agent
        parts.append(f"agent={agent}")
    tk.update_ticket(path, fields=fields, feed="assigned " + " ".join(parts), by=by)
    return {"id": ticket_id, "path": str(path), "assignee": assignee, "agent": agent}


EDITABLE_FM = ("priority", "kind")
_CHOICES = {"priority": tk.PRIORITIES, "kind": tk.KINDS}
_LOCKED = ("id", "status", "project")
_TEXT_KEYS = ("goal", "verdict", "title", "criteria", "add_criteria", "parent")
_LIST_KEYS = ("blocked_by", "remove_blocked_by", "owns", "remove_owns")


def _checklist(items: list[str]) -> str:
    return "\n".join(f"- [ ] {i.strip()}" for i in items if i.strip())


def _merge(current: list[str], add: list[str] | None, remove: list[str] | None) -> list[str]:
    out = [x for x in current if x not in set(remove or [])]
    return out + [x for x in (add or []) if x not in out]


def edit(vault: Path, ticket_id: str, changes: dict[str, Any], by: str | None = None) -> dict[str, Any]:
    """Edit a ticket's body and descriptive fields atomically under its lock.
    Raises BoardError for a locked or unknown field and `Refused` (file
    untouched) when the new `blocked_by` names no ticket or the new `owns`
    collides with another in-progress ticket of the project."""
    changes = {k: v for k, v in changes.items() if v is not None and v != []}
    for key in changes:
        if key not in EDITABLE_FM + _TEXT_KEYS + _LIST_KEYS:
            if key in _LOCKED:
                hint = "use `mb board move`" if key == "status" else "it is fixed at creation"
                raise BoardError(f"'{key}' cannot be edited: {hint}")
            raise BoardError(f"unknown field '{key}'")
    if not changes:
        raise BoardError("nothing to edit")
    for key, allowed in _CHOICES.items():
        if key in changes and changes[key] not in allowed:
            raise BoardError(f"{key} '{changes[key]}' is not one of: {', '.join(allowed)}")
    path = _need(vault, ticket_id)

    def prospective(parsed: dict[str, Any]) -> dict[str, Any]:
        fm = dict(_fm(parsed))
        for key in EDITABLE_FM:
            if key in changes:
                fm[key] = changes[key]
        if "blocked_by" in changes or "remove_blocked_by" in changes:
            fm["blocked_by"] = _merge(list(fm.get("blocked_by") or []),
                                      changes.get("blocked_by"), changes.get("remove_blocked_by"))
        if "owns" in changes or "remove_owns" in changes:
            fm["owns"] = _merge(list(fm.get("owns") or []),
                                changes.get("owns"), changes.get("remove_owns"))
        if "title" in changes:
            fm["title"] = f"{ticket_id} {changes['title'].strip()}"
        if "parent" in changes:
            fm["parent"] = changes["parent"]
        return fm

    def gate(parsed: dict[str, Any]) -> None:
        for ref in [*(changes.get("blocked_by") or []), *([changes["parent"]] if "parent" in changes else [])]:
            if not tk._ID_RE.fullmatch(ref):
                raise BoardError(f"'{ref}' is not a ticket id like DD-12")
        everyone = tk.iter_tickets(vault)
        known = _by_id(everyone)
        found = [Violation("unknown_ref", f"{ref} is not a ticket")
                 for ref in [*(changes.get("blocked_by") or []),
                             *([changes["parent"]] if "parent" in changes else [])]
                 if ref not in known]
        fm = prospective(parsed)
        if changes.get("parent") == ticket_id:
            found.append(Violation("unknown_ref", f"{ticket_id} cannot be its own parent"))
        graph = {_id(t): list(_fm(t).get("blocked_by") or []) for t in everyone}
        graph[ticket_id] = list(fm.get("blocked_by") or [])
        cycle = find_cycle(graph)
        if cycle and ticket_id in cycle and ("blocked_by" in changes or "remove_blocked_by" in changes):
            found.append(Violation("blocked_cycle", "blocked_by would loop: " + " -> ".join(cycle)))
        if fm.get("status") == "in-progress" and fm.get("kind") != "epic" and (
                "owns" in changes or "kind" in changes):
            for other in _in_progress(everyone, str(fm.get("project")), skip=ticket_id):
                clash = owns_collide(list(fm.get("owns") or []),
                                     list(_fm(other).get("owns") or []))
                if clash:
                    found.append(Violation("owns_overlap", f"{ticket_id} would own {clash[0][0]}, "
                                           f"which {_id(other)} (in progress) owns as {clash[0][1]}"))
        if found:
            raise Refused(found)

    def fields(parsed: dict[str, Any]) -> dict[str, Any]:
        fm, old = prospective(parsed), _fm(parsed)
        return {k: fm[k] for k in (*EDITABLE_FM, "blocked_by", "owns", "title", "parent")
                if fm.get(k) != old.get(k)}

    def rewrite(text: str, parsed: dict[str, Any]) -> str:
        if "goal" in changes:
            text = tk.replace_section(text, tk.SECTION_GOAL, changes["goal"])
        if "verdict" in changes:
            text = tk.replace_section(text, tk.SECTION_VERDICT, changes["verdict"])
        if "criteria" in changes or "add_criteria" in changes:
            have = (_checklist(changes["criteria"]) if "criteria" in changes
                    else parsed["sections"][tk.SECTION_CRITERIA])
            keep = have if criteria_filled(have) else ""
            extra = _checklist(changes.get("add_criteria") or [])
            text = tk.replace_section(text, tk.SECTION_CRITERIA,
                                      "\n".join(x for x in (keep, extra) if x))
        if "title" in changes:
            text = re.sub(r"^# .*$", lambda _m: f"# {ticket_id} {changes['title'].strip()}",
                          text, count=1, flags=re.M)
        return text

    labels = ", ".join(sorted(changes))
    result = tk.update_ticket(path, fields=fields, check=gate, rewrite=rewrite, by=by,
                              feed=f"edited: {labels} by {by or 'lead'}")
    return {"id": ticket_id, "edited": sorted(changes), "path": str(result["path"])}
