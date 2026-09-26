"""`mb code` — the structural code graph (decision CODE-GRAPH D1).

Presentation only: the answers come from `codegraph.query`, which reads a
built graph and never parses code. `build` is the one verb that parses; its
import is deferred so every other verb runs on an interpreter without the
parser installed (the search daemon's and `mb`'s Python may not have it).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_OFF = "[mb] the code graph is off — `mb config set code_graph_enabled true`"


def _where(cfg, args) -> Path:
    if getattr(args, "project", None):
        from ..machine.projects import resolve_selector
        projects = cfg.data.get("projects") or {}
        ids = resolve_selector(projects, args.project)
        if len(ids) != 1 or not isinstance(projects.get(ids[0]), dict):
            raise KeyError(f"--project {args.project!r} must name exactly one registered project")
        return Path(projects[ids[0]]["path"])
    return Path(os.getcwd())


def _emit(args, payload: dict[str, Any], text: str) -> None:
    print(json.dumps(payload, ensure_ascii=False) if args.json else text)


def _fmt_page(p: dict[str, Any], line) -> list[str]:
    out = [line(item) for item in p["items"]]
    if p["has_more"]:
        out.append(f"  … {p['total'] if p['total'] is not None else 'more'} in total; "
                   f"--offset {p['next_offset']} for the next page")
    return out


def cmd_code(cfg, args) -> int:
    from ..codegraph import query, store
    from ..codegraph.scope import GraphError

    action = args.code_action
    if action == "status":
        units = store.built_units(cfg)
        payload = {"enabled": bool(cfg.data.get("code_graph_enabled")), "units": units}
        lines = [f"code graph: {'on' if payload['enabled'] else 'off'}"]
        lines += [f"  {u['key']}: {u['root']}" for u in units] or ["  no graph built yet (`mb code build`)"]
        _emit(args, payload, "\n".join(lines))
        return 0
    if not cfg.data.get("code_graph_enabled"):
        print(_OFF, file=sys.stderr)
        return 2
    try:
        if action == "build":
            return _build(cfg, args)
        g = query.open_graph(cfg, _where(cfg, args))
        try:
            return _answer(g, args)
        finally:
            g.con.close()
    except query.Ambiguous as exc:
        print(f"[mb] {exc}", file=sys.stderr)
        for c in exc.candidates[:20]:
            print(f"  {c}", file=sys.stderr)
        return 2
    except (GraphError, KeyError) as exc:
        print(f"[mb] {exc}", file=sys.stderr)
        return 2


def _build(cfg, args) -> int:
    try:
        from ..codegraph import build, scope
    except ImportError as exc:       # the parser wheels are not in this interpreter
        print(f"[mb] cannot build here: {exc}. Install the parser into {sys.executable}:\n"
              "  python -m pip install --require-hashes -r requirements-graph.txt", file=sys.stderr)
        return 2
    units = scope.discover_units(cfg)
    if not args.all:
        where = os.path.normcase(os.path.realpath(str(_where(cfg, args))))
        inside = [u for u in units
                  if where == os.path.normcase(os.path.realpath(u.root))
                  or where.startswith(os.path.normcase(os.path.realpath(u.root)) + os.sep)]
        if not inside:
            print(f"[mb] {where} is in no graph unit (a child of code_repos_dir or a "
                  "code_graph_extra_roots entry); use --all to build every unit", file=sys.stderr)
            return 2
        units = [max(inside, key=lambda u: len(str(u.root)))]
    results = []
    for unit in units:
        s = build.build_unit(cfg, unit)
        results.append(s)
        if not args.json:
            pending = f", {len(s['gc_pending'])} old generation(s) still held open" if s["gc_pending"] else ""
            print(f"{s['unit']}: {s['files']} files, {s['nodes']} nodes, "
                  f"{sum(s['edges'].values())} edges, {s['unresolved']} unresolved, {s['seconds']}s{pending}")
    if args.json:
        print(json.dumps({"built": results}, ensure_ascii=False))
    return 0


def _answer(g, args) -> int:
    from ..codegraph import query
    action = args.code_action
    if action == "schema":
        data = query.schema(g)
        _emit(args, query.envelope(action, g, data),
              "\n".join(f"{k}: {v}" for k, v in data.items() if k != "meta"))
        return 0
    if action == "outline":
        items = query.outline(g.con, args.target)
        _emit(args, query.envelope(action, g, {"file": args.target, "symbols": items}),
              "\n".join(f"{n['spans'][0][0]:>5}  {n['kind']:<9} {n['qname'].split('::', 1)[1]}"
                        for n in items))
        return 0
    if action == "find":
        data = query.find(g.con, args.target, args.limit, args.offset)
        _emit(args, query.envelope(action, g, data),
              "\n".join(_fmt_page(data["matches"], lambda n: f"  {n['kind']:<9} {n['qname']}")))
        return 0
    symbol = query.resolve_symbol(g.con, args.target)
    if action == "snippet":
        node = query.snippet(g, symbol)
        _emit(args, query.envelope(action, g, {"symbol": node}),
              "\n\n".join(f"# {node['file']}:{a}-{b}\n{code}"
                          for (a, b), code in zip(node["spans"], node["code"], strict=True)))
        return 0
    if action == "callers":
        data = query.callers(g.con, symbol, args.limit, args.offset)
        cand = data["unresolved_candidates"]
        text = [f"callers of {symbol}:"]
        text += _fmt_page(data["callers"], lambda c: f"  {c['file']}:{c['line']}  {c['caller']}  [{c['tier']}]")
        if cand["total"]:
            text.append(f"unresolved call sites with the same name ({cand['total']}) — "
                        "possible callers the resolver could not place:")
            text += _fmt_page(cand, lambda c: f"  {c['file']}:{c['line']}  {c['caller']}  (receiver: {c['receiver']})")
        _emit(args, query.envelope(action, g, data), "\n".join(text))
        return 0
    if action == "callees":
        data = query.callees(g.con, symbol, args.limit, args.offset)
        text = [f"calls made by {symbol}:"]
        text += _fmt_page(data["callees"], lambda c: f"  {c['line']:>5}  {c['callee']}  [{c['tier']}]")
        if data["unresolved_calls"]:
            text.append(f"unresolved ({len(data['unresolved_calls'])}):")
            text += [f"  {u['line']:>5}  {u['receiver'] + '.' if u['receiver'] else ''}{u['name']}"
                     for u in data["unresolved_calls"]]
        _emit(args, query.envelope(action, g, data), "\n".join(text))
        return 0
    if action == "trace":
        data = query.trace(g.con, symbol, args.direction, args.depth, args.limit)
        _emit(args, query.envelope(action, g, data),
              "\n".join([f"{args.direction} of {symbol} (depth ≤{data['depth']}):"]
                        + _fmt_page(data["reached"], lambda r: f"  {'  ' * (r['depth'] - 1)}{r['symbol']}")))
        return 0
    print(f"[mb] unknown code action: {action}", file=sys.stderr)
    return 2
