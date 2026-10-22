"""`mb board ...`: the ticket board from the command line (logic: knowledge/board.py)."""
from __future__ import annotations

import json
import sys
from typing import Any

from ..knowledge import board as bd
from ..knowledge import tickets as tk


def _row(t: dict[str, Any]) -> dict[str, Any]:
    fm = t["frontmatter"]
    return {"id": fm["id"], "title": str(fm.get("title", "")).removeprefix(f"{fm['id']} "),
            "project": fm.get("project"), "kind": fm.get("kind"), "status": fm.get("status"),
            "priority": fm.get("priority"), "assignee": fm.get("assignee"),
            "agent": fm.get("agent"), "blocked_by": fm.get("blocked_by") or [],
            "owns": fm.get("owns") or []}


def _out(args, payload: dict[str, Any], human: str) -> None:
    print(json.dumps(payload, ensure_ascii=False) if getattr(args, "json", False) else human)


def _fail(args, message: str, extra: dict[str, Any] | None = None) -> int:
    if getattr(args, "json", False):
        print(json.dumps({"ok": False, "error": message, **(extra or {})}, ensure_ascii=False))
    else:
        print(f"[mb] {message}", file=sys.stderr)
    return 1


def cmd_board(cfg, args) -> int:
    action = getattr(args, "board_action", None) or "columns"
    project = getattr(args, "project", None)
    vault = cfg.vault
    try:
        if action == "columns":
            cols = bd.columns(tk.iter_tickets(vault, project))
            payload = {"project": project,
                       "columns": {s: [_row(t) for t in ts] for s, ts in cols.items()}}
            lines = []
            for status, ts in cols.items():
                lines.append(f"{status.upper()} ({len(ts)})")
                lines += [f"  {_row(t)['id']:<9} {_row(t)['priority']}  {_row(t)['title']}"
                          for t in ts]
            _out(args, payload, "\n".join(lines))
            return 0
        if action == "next":
            rows = [_row(t) for t in bd.next_ready(tk.iter_tickets(vault, project))]
            _out(args, {"next": rows},
                 "\n".join(f"{r['id']:<9} {r['priority']}  {r['title']}" for r in rows)
                 or "(nothing ready)")
            return 0
        if action == "show":
            path = tk.find_ticket(vault, args.id)
            if path is None:
                return _fail(args, f"no ticket {args.id}")
            t = tk.parse_ticket(path)
            _out(args, {"id": args.id, "path": str(path), "frontmatter": t["frontmatter"],
                        "sections": t["sections"]}, path.read_text(encoding="utf-8"))
            return 0
        if action == "move":
            res = bd.move(vault, cfg.get, args.id, args.status,
                          force_reason=args.force_reason, by=args.by)
            _out(args, {"ok": True, **res}, f"{res['id']}: {res['from']} -> {res['to']}"
                 + (f" (forced: {', '.join(res['forced'])})" if res["forced"] else ""))
            return 0
        if action == "note":
            res = bd.add_note(vault, args.id, args.text, by=args.by)
            _out(args, {"ok": True, **res}, f"noted {args.id}")
            return 0
        if action == "assign":
            res = bd.assign(vault, args.id, assignee=args.assignee, agent=args.agent, by=args.by)
            _out(args, {"ok": True, **res}, f"assigned {args.id}")
            return 0
        if action == "edit":
            changes = {"parent": args.parent, "goal": args.goal, "verdict": args.verdict, "title": args.title,
                       "priority": args.priority, "kind": args.kind,
                       "criteria": args.criteria, "add_criteria": args.add_criterion,
                       "blocked_by": args.blocked_by, "remove_blocked_by": args.remove_blocked_by,
                       "owns": args.owns, "remove_owns": args.remove_owns}
            res = bd.edit(vault, args.id, changes, by=args.by)
            _out(args, {"ok": True, **res}, f"edited {args.id}: {', '.join(res['edited'])}")
            return 0
        if action == "check":
            found = bd.check_all(tk.iter_tickets(vault, project),
                                 lambda p: bd.wip_limit(cfg.get, p))
            _out(args, {"ok": not found, "violations": found},
                 "\n".join(f"{v['id']}: [{v['code']}] {v['message']}" for v in found)
                 or "board OK")
            return 1 if found else 0
    except bd.Refused as exc:
        vs = [v.as_dict() for v in exc.violations]
        return _fail(args, "; ".join(f"[{v['code']}] {v['message']}" for v in vs),
                     {"refused": vs, "forceable": bd.can_force(exc.violations)})
    except (bd.BoardError, tk.TicketError) as exc:
        return _fail(args, str(exc))
    return 2
