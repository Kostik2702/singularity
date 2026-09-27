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
        from ..codegraph import update
        card = query.locate(cfg, _where(cfg, args))
        if action == "update":
            return _update(cfg, args, card, update)
        # Refresh before the answer (CODE-GRAPH P3): the tree is compared with
        # the graph and small changes are written in first. --no-refresh skips
        # the comparison but still reports what the graph knows to be stale.
        fr = None if args.no_refresh else update.refresh(cfg, card["key"])
        if action == "impact" and fr is not None and (fr.stale_files or fr.stale_all):
            fr = _whole_diff(cfg, card, fr, update)
        g = query.open_card(cfg, card)
        try:
            if fr is None:
                fr = update.known_stale(g.con)
            return _answer(g, args, fr)
        finally:
            g.con.close()
    except query.Ambiguous as exc:
        print(f"[mb] {exc}", file=sys.stderr)
        for c in exc.candidates[:20]:
            print(f"  {c}", file=sys.stderr)
        return 2
    except (GraphError, KeyError, RuntimeError) as exc:     # RuntimeError: git failed, impact refused
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


def _update(cfg, args, card, update) -> int:
    try:
        result = update.update_unit(cfg, card["key"])
    except ImportError as exc:       # the parser wheels are not in this interpreter
        print(f"[mb] cannot update here: {exc}. Install the parser into {sys.executable}:\n"
              "  python -m pip install --require-hashes -r requirements-graph.txt", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, default=str))
    elif result["mode"] == "update":
        print(f"{card['key']}: {len(result['updated'])} changed files, {result['resolved']} files "
              f"resolved again, {len(result['stale'])} stale")
    else:
        print(f"{card['key']}: {result['mode']} ({result.get('why')})")
    return 0


def _whole_diff(cfg, card, fr, update):
    """impact walks the graph from the changed files, so all of them must be
    in it (decision D1): whatever the refresh left stale is updated now, not
    in the background -- or, past IMPACT_MAX_FILES, refused with the count."""
    from ..codegraph.impact import IMPACT_MAX_FILES, ImpactRefused
    if len(fr.stale_files) > IMPACT_MAX_FILES:
        raise ImpactRefused(f"{len(fr.stale_files)} files differ from the graph; impact needs all of them in "
                            f"it and does that for up to {IMPACT_MAX_FILES}: run `mb code update` first")
    update.update_unit(cfg, card["key"])
    return update.refresh(cfg, card["key"])


def _freshness_note(fr) -> str | None:
    """One line on stderr for the text answers; JSON carries it in the envelope."""
    parts = []
    if fr.updated:
        parts.append(f"updated {len(fr.updated)} changed file(s) first")
    if fr.stale_all:
        parts.append(f"the graph may be stale: {fr.stale_all}")
    elif fr.stale_files:
        shown = ", ".join(fr.stale_files[:5]) + (" …" if len(fr.stale_files) > 5 else "")
        parts.append(f"{len(fr.stale_files)} stale file(s), answers there may be off: {shown}")
    if fr.background:
        parts.append("a background `mb code update` is catching up")
    if fr.note and (fr.stale_files or fr.stale_all or not fr.checked):
        parts.append(fr.note)
    return "[mb] " + "; ".join(parts) if parts else None


def _whole_graph(g, args, env) -> int:
    from ..codegraph import analysis
    action = args.code_action
    if action == "impact":
        from ..codegraph.impact import impact
        data = impact(g.con, g.root, args.base, args.depth, args.limit, args.offset)
        cov = data["coverage"]
        text = [f"impact against {data['base']['what']} ({data['base']['commit'][:10]}): "
                f"{len(data['changed']['symbols'])} changed symbol(s) in {cov['code_files']} code file(s)"]
        text += [f"  {s['change']:<12} {s['symbol']}" + (f"  (was {s['was']})" if s.get("was") else "")
                 for s in data["changed"]["symbols"]]
        text.append(f"reached through callers (depth ≤{cov['depth']}):")
        text += _fmt_page(data["impacted"], lambda i: f"  {'  ' * (i['depth'] - 1)}{i['symbol']}"
                                                      + ("  [test]" if i["test"] else ""))
        text.append("tests: " + (", ".join(data["tests"]) or "none reached"))
        if data["possibly_broken"]:
            text.append(f"call sites of deleted names, now unresolved ({len(data['possibly_broken'])}):")
            text += [f"  {b['file']}:{b['line']}  {b['caller']} -> {b['name']}" for b in data["possibly_broken"][:20]]
        if cov["outside_graph"]:
            text.append(f"not in the graph ({len(cov['outside_graph'])}): " + ", ".join(cov["outside_graph"][:8])
                        + (" …" if len(cov["outside_graph"]) > 8 else ""))
        text.append(f"unplaced call sites of the changed names: {cov['unresolved_same_name_sites']}")
        _emit(args, env(action, data), "\n".join(text))
        return 0
    if action == "arch":
        data = analysis.arch_view(g.con, g.meta, args.top)
        text = [f"{data['communities']['total']} communities, {data['isolated_files']} isolated file(s)"]
        text += [f"  #{c['community']:<5} {c['files']:>6} files  {c['label']}   core: {', '.join(c['core_files'][:3])}"
                 for c in data["communities"]["items"]]
        text.append("coupling:")
        text += [f"  {k['edges']:>6}  #{k['communities'][0]} {k['between'][0]} <-> "
                 f"#{k['communities'][1]} {k['between'][1]}" for k in data["coupling"]]
        text.append("most called:")
        text += [f"  {h['callers']:>6}  {h['symbol']}" for h in data["hotspot_symbols"]]
        if data["computed"]["note"]:
            text.append(f"note: {data['computed']['note']}")
        _emit(args, env(action, data), "\n".join(text))
        return 0
    data = analysis.deadcode(g.con, g.root, g.meta, args.limit, args.offset, args.unknown)
    text = [f"{data['dead']['total']} dead, {data['unknown']['total']} unknown of {data['candidates']} "
            f"uncalled definitions ({data['entry_points']} entry points declared)"]
    text += _fmt_page(data["dead"], lambda d: f"  {d['file']}:{d['line']}  {d['kind']:<8} {d['qname'].split('::', 1)[1]}")
    text.append("unknown, by reason: " + ", ".join(f"{k} {n}" for k, n in data["unknown"]["by_reason"].items()))
    if args.unknown:
        text += _fmt_page(data["unknown"]["items"],
                          lambda d: f"  {d['file']}:{d['line']}  {d['qname'].split('::', 1)[1]}  — {d['why']}")
    _emit(args, env(action, data), "\n".join(text))
    return 0


def _schema_text(data: dict[str, Any]) -> str:
    from ..codegraph import store
    text = [f"{k}: {v}" for k, v in data.items() if k not in ("meta", "views", "views_version")]
    have = data["views_version"]
    text.append(f"views of `mb code sql` (v{store.VIEWS_VERSION}"
                + ("" if have == store.VIEWS_VERSION else f"; this graph has {have}: rebuild it to query them")
                + "):")
    for name, view in data["views"].items():
        text.append(f"  {name}: {view['row']}")
        width = max(len(c) for c in view["columns"])
        text += [f"    {c:<{width}}  {meaning}" for c, meaning in view["columns"].items()]
    return "\n".join(text)


def _sql(g, args, env) -> int:
    """`mb code sql`. The SQL comes from --sql-file or stdin, never from the
    command line, where the shell has its say on quotes and `*` first."""
    from ..codegraph import sql
    data = sql.run(g.con, _sql_text(args), limit=args.limit, budget=args.timeout)
    _emit(args, env("sql", data), "\n".join(_table(data["columns"], data["rows"])))
    return 0


def _sql_text(args) -> str:
    from ..codegraph.sql import SqlRefused
    try:
        if args.sql_file:
            return Path(args.sql_file).read_text(encoding="utf-8-sig")
        if sys.stdin is None or sys.stdin.isatty():
            raise SqlRefused("no SQL: pass --sql-file FILE, or pipe it in (`mb code sql < query.sql`)")
        raw = getattr(sys.stdin, "buffer", None)
        # strict: a byte that is not UTF-8 would otherwise change a literal silently
        return raw.read().decode("utf-8-sig") if raw is not None else sys.stdin.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise SqlRefused(f"cannot read the SQL: {exc}") from None


def _table(columns: list[str], rows: dict[str, Any]) -> list[str]:
    """An aligned text table: a column of numbers right-aligned, NULL spelled
    out, line breaks inside a value folded (--json has every value exact), and
    a cut answer says it was cut."""
    def show(v: Any) -> str:
        return "NULL" if v is None else " ".join(str(v).splitlines())

    cells = [[show(v) for v in row] for row in rows["items"]]
    widths = [max([len(c)] + [len(r[i]) for r in cells]) for i, c in enumerate(columns)]
    numeric = [bool(cells) and all(isinstance(row[i], (int, float)) or row[i] is None for row in rows["items"])
               for i in range(len(columns))]

    def line(values: list[str]) -> str:
        return "  ".join(v.rjust(w) if num else v.ljust(w)
                         for v, w, num in zip(values, widths, numeric, strict=True)).rstrip()

    out = [line(columns), line(["-" * w for w in widths]), *(line(r) for r in cells)]
    n = len(cells)
    out.append(f"… more rows past --limit {rows['limit']}: raise it, or narrow the query" if rows["has_more"]
               else f"({n} row{'' if n == 1 else 's'})")
    return out


def _answer(g, args, fr) -> int:
    from ..codegraph import query

    def env(action, data):
        return query.envelope(action, g, data, {**fr.as_json(), "all_stale": fr.stale_files})

    note = _freshness_note(fr)
    if note and not args.json:
        print(note, file=sys.stderr)
    action = args.code_action
    if action == "schema":
        data = query.schema(g)
        _emit(args, env(action, data), _schema_text(data))
        return 0
    if action == "sql":
        return _sql(g, args, env)
    if action == "outline":
        items = query.outline(g.con, args.target)
        _emit(args, env(action, {"file": args.target, "symbols": items}),
              "\n".join(f"{n['spans'][0][0]:>5}  {n['kind']:<9} {n['qname'].split('::', 1)[1]}"
                        for n in items))
        return 0
    if action == "find":
        data = query.find(g.con, args.target, args.limit, args.offset)
        _emit(args, env(action, data),
              "\n".join(_fmt_page(data["matches"], lambda n: f"  {n['kind']:<9} {n['qname']}")))
        return 0
    if action in ("impact", "arch", "deadcode"):
        return _whole_graph(g, args, env)
    symbol = query.resolve_symbol(g.con, args.target)
    if action == "snippet":
        node = query.snippet(g, symbol)
        _emit(args, env(action, {"symbol": node}),
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
        _emit(args, env(action, data), "\n".join(text))
        return 0
    if action == "callees":
        data = query.callees(g.con, symbol, args.limit, args.offset)
        text = [f"calls made by {symbol}:"]
        text += _fmt_page(data["callees"], lambda c: f"  {c['line']:>5}  {c['callee']}  [{c['tier']}]")
        if data["unresolved_calls"]:
            text.append(f"unresolved ({len(data['unresolved_calls'])}):")
            text += [f"  {u['line']:>5}  {u['receiver'] + '.' if u['receiver'] else ''}{u['name']}"
                     for u in data["unresolved_calls"]]
        _emit(args, env(action, data), "\n".join(text))
        return 0
    if action == "trace":
        data = query.trace(g.con, symbol, args.direction, args.depth, args.limit)
        _emit(args, env(action, data),
              "\n".join([f"{args.direction} of {symbol} (depth ≤{data['depth']}):"]
                        + _fmt_page(data["reached"], lambda r: f"  {'  ' * (r['depth'] - 1)}{r['symbol']}")))
        return 0
    print(f"[mb] unknown code action: {action}", file=sys.stderr)
    return 2
