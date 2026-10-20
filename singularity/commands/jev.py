"""`mb jev ...`: Jev (TypeSafe's decision API) from the command line."""
from __future__ import annotations

import json
import os
import sys
import time


def cmd_jev(cfg, args) -> int:
    action = args.jev_action
    if action == "key":
        return _key(args)
    if action == "status":
        return _status(cfg, args)
    if action in ("on", "off"):
        return _switch(cfg, action == "on")
    if action == "ask":
        return _ask(cfg, args)
    if action == "pick":
        return _pick(cfg, args)
    if action == "log":
        return _log(cfg, args)
    return 2


def _session() -> str | None:
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or None


def _key(args) -> int:
    from ..jev import client
    from ..platform import secrets

    if getattr(args, "rest", None):
        # a pasted key is what lands here; the refusal must not quote it
        print("[mb] refusing: the key is read from stdin, never from arguments "
              "(pbpaste | mb jev key set)", file=sys.stderr)
        return 2
    if args.key_action == "status":
        key = client.resolve_key()
        found = key is not None
        print(f"key: {'found' if found else 'not found'} "
              f"(backend: {_key_backend(key) or secrets.backend()})")
        return 0 if found else 1
    if args.key_action == "delete":
        removed = secrets.delete_secret(client.SECRET_NAME)
        print("key deleted" if removed else "no stored key")
        return 0
    # set: the value comes from STDIN only and the name is fixed, so there is no
    # path on which a pasted key can end up as the name of a secret.
    if sys.stdin.isatty():
        print("[mb] pipe the key in: pbpaste | mb jev key set  "
              "(Windows: Get-Clipboard | mb jev key set; Linux: xclip -o -selection clipboard | mb jev key set)",
              file=sys.stderr)
        return 2
    value = sys.stdin.read().strip()
    if not value or any(ch.isspace() for ch in value):
        print("[mb] refusing: stdin must hold exactly one key, with no inner whitespace",
              file=sys.stderr)
        return 2
    try:
        backend = secrets.set_secret(client.SECRET_NAME, value)
    except secrets.SecretsError as exc:
        print(f"[mb] {str(exc).replace(value, '***')}", file=sys.stderr)
        return 1
    print(f"stored in {backend}")
    return 0


def _key_backend(key: str | None) -> str | None:
    """Where the key that `resolve_key` returned comes from, or None without one."""
    from ..jev import client
    from ..platform import secrets

    if (os.environ.get(client.KEY_ENV) or "").strip():
        return "env"
    return secrets.backend() if key else None


def status_payload(cfg) -> dict:
    from .. import jev
    from ..jev import client, log

    session = _session()
    key = client.resolve_key()
    total = log.stats(cfg)
    budget = cfg.get("jev_budget_usd")
    return {
        "enabled": jev.enabled(cfg),
        "key_found": key is not None,
        "key_backend": _key_backend(key),
        "model": client.MODEL,
        "journal": str(log.path(cfg)),
        "budget_usd": budget,
        "total_cost": total["cost_total"],
        "session_cost": log.stats(cfg, session)["cost_total"] if session else None,
        "session": session,
        "balance_usd": budget - total["cost_total"] if budget is not None else None,
        "by_size": total["by_size"],
        "decided_self": total["decided_self"],
        "errors": total["errors"],
        "calls": total["calls"],
        "cost_by_kind": total["cost_by_kind"],
    }


def _status(cfg, args) -> int:
    data = status_payload(cfg)
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
        return 0
    balance = (f"${data['balance_usd']:.6f}" if data["balance_usd"] is not None
               else "unknown (set jev_budget_usd)")
    print(f"jev: {'enabled' if data['enabled'] else 'disabled'} (model {data['model']})")
    print(f"key: {'found' if data['key_found'] else 'not found'}"
          + (f" ({data['key_backend']})" if data["key_backend"] else ""))
    print(f"journal: {data['journal']}")
    sizes = ", ".join(f"{k}={v}" for k, v in data["by_size"].items())
    print(f"calls: {data['calls']} ({sizes}); decided by self: {data['decided_self']}; "
          f"errors: {data['errors']}")
    session = f"; this session ${data['session_cost']:.6f}" if data["session"] else ""
    print(f"cost: ${data['total_cost']:.6f} all time{session}")
    print(f"balance: {balance}")
    return 0


def _switch(cfg, on: bool) -> int:
    from ..jev import client

    if on:
        cfg.data["jev_enabled"] = True
    else:
        cfg.data.pop("jev_enabled", None)
    cfg.save()
    print(f"jev {'enabled' if on else 'disabled'}")
    if on and client.resolve_key() is None:
        print("[mb] no key yet: pbpaste | mb jev key set", file=sys.stderr)
    return 0


def _redact_tree(value):
    from ..knowledge.redact import redact

    if isinstance(value, str):
        return redact(value).text
    if isinstance(value, list):
        return [_redact_tree(v) for v in value]
    if isinstance(value, dict):
        return {k: _redact_tree(v) for k, v in value.items()}
    return value


def _ask(cfg, args) -> int:
    from .. import jev
    from ..jev import client, log

    def fail(kind: str, status=None, body: str = "") -> int:
        print(json.dumps({"error": {"kind": kind, "status": status, "body": body[:2000]}},
                         ensure_ascii=False))
        return 1

    if not jev.enabled(cfg):
        return fail("off", body="jev_enabled is off (mb jev on)")
    try:
        request = json.loads(sys.stdin.read())
        state, questions = request["state"], request["questions"]
        if not isinstance(questions, dict) or not questions:
            raise ValueError("questions must be a non-empty object")
    except (ValueError, KeyError, TypeError) as exc:
        return fail("bad_json", body=f"stdin must be {{\"state\":..,\"questions\":{{..}}}}: {exc}")
    text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
    started = time.monotonic()
    try:
        reply = client.ask(_redact_tree(state), _redact_tree(questions), timeout=args.timeout,
                           deadline=started + args.timeout)
    except client.JevError as exc:
        log.append(cfg, "ask", text=text, session=_session(), error=exc.tag)
        return fail(exc.kind, exc.status, exc.body)
    log.append(cfg, "ask", text=text, session=_session(), cost=reply["cost"], ms=reply["ms"],
               model=reply["model"], request_id=reply["request_id"])
    print(json.dumps({k: reply[k] for k in ("answers", "ms", "cost", "model", "usage")},
                     ensure_ascii=False))
    return 0


def _pick(cfg, args) -> int:
    from ..jev import pick

    text = " ".join(args.text).strip() if args.text else ""
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    if not text:
        print("pick: decide yourself (empty brief)")
        return 0
    print(pick.pick(cfg, text, _session()))
    return 0


def _log(cfg, args) -> int:
    from ..jev import log

    rows = log.read(cfg)
    if args.session:
        rows = [r for r in rows if r.get("session") == args.session]
    for row in rows[-args.tail:] if args.tail > 0 else rows:
        print(json.dumps(row, ensure_ascii=False))
    return 0
