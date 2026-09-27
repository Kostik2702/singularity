"""Reports as text — the half of a command that talks to a human.

Every `mb` report already produced two renderings of the same facts: a `--json`
branch and a text branch, written side by side inside the domain module that
computed them. That coupling had a price beyond tidiness: the collectors could
not be reused by a daemon, a hook or another command without their printing
coming along, and adding a field meant remembering to touch two places in one
function.

So the split is: domain modules RETURN data (`status.snapshot()`,
`curator.analyze()`, `gaps.report()`), the CLI decides between `--json` and
these functions, and the JSON stays exactly what the collector returned — the
one contract skills and tests already depend on.

Each function here takes the collector's dict and returns text; none of them
prints, so they can be tested by comparing strings instead of capturing
streams.
"""
from __future__ import annotations

from typing import Any


def status(snap: dict[str, Any]) -> str:
    out = [f"vault: {snap['vault']}"]
    idx = snap["index"]
    if idx.get("present"):
        out.append(f"index: {idx.get('chunks')} chunks, {idx.get('layout')}, "
                   f"age {idx.get('age_hours')}h, schema {idx.get('chunk_schema')}")
    else:
        out.append(f"index: {idx.get('error', 'absent')}")
    dae = snap["daemon"]
    out.append(f"daemon: {'up' if dae.get('ping') else 'down'} "
               f"(pid {dae.get('pid')}, orphan={dae.get('orphan')})")
    if dae.get("code_stale"):
        out.append("  ! serving code older than the files on disk — "
                   "`mb daemon restart` (reload does not re-read modules)")
    metrics = dae.get("metrics") or {}
    if metrics:
        search_ms = metrics.get("search_ms") or {}
        parts = [f"{metrics.get('searches', 0)} searches", f"{metrics.get('errors', 0)} errors",
                 f"{metrics.get('reloads', 0)} reloads"]
        if search_ms:
            parts.append(f"p50 {search_ms['p50']}ms / p95 {search_ms['p95']}ms")
        out.append(f"  since start ({metrics.get('uptime_s', 0)}s): " + ", ".join(parts))
    w = snap["watcher"]
    out.append(f"watcher: {'alive' if w.get('alive') else 'not running'}"
               + (f" (heartbeat {w.get('heartbeat_age_s')}s ago)"
                  if w.get("heartbeat_age_s") else ""))
    scheduler = snap["scheduler"]
    if isinstance(scheduler, dict) and "error" not in scheduler:
        for name, s in scheduler.items():
            out.append(f"scheduler[{name}]: registered={s.get('registered')}")
    src = snap["sources"]
    if src.get("present"):
        out.append(f"sources: {src['documents']} documents "
                   f"({src['unprocessed']} unprocessed), {src['chunks']} chunks"
                   + ("" if src.get("corpus_enabled") else ", corpus DISABLED"))
    code = snap["code"]
    if code.get("configured"):
        if code.get("present"):
            # A built-but-empty corpus is not a small healthy number, it is a
            # broken feature (issue #13) — do not print it in the same neutral
            # tone as a real count.
            empty = "" if code.get("files") else "  <- EMPTY, code search finds nothing"
            out.append(f"code: {code['chunks']} chunks from {code['files']} files, "
                       f"age {code['age_hours']}h{empty}")
        else:
            out.append("code: configured but not built (`mb index rebuild --code`)")
    out.append(f"disk(.mb): {snap['disk'].get('mb_dir_mb')} MB")
    h = snap["hooks"]
    if "error" not in h:
        desired = "enabled" if h.get("configured") else "disabled"
        clients = ", ".join(f"{n}={'yes' if c['installed'] else 'no'}"
                            for n, c in h.get("clients", {}).items())
        out.append(f"capture hooks: {desired} (source: {h.get('source')}); "
                   f"installed: {clients}")
    return "\n".join(out)


CURATOR_SECTIONS = (
    ("broken_links", "Broken wikilinks"),
    ("missing_frontmatter", "Missing mandatory frontmatter"),
    ("decision_no_owner", "Decisions without owner/source"),
    ("duplicate_titles", "Duplicate titles"),
    ("source_not_in_registry", "Notes citing an unknown source_id"),
)


def curator(payload: dict[str, Any], *, orphan_source_days: int, stale_days: int,
            suggest_threshold: float, duplicate_threshold: float,
            supersession_gap_days: int) -> str:
    """Thresholds come in as arguments: they are the domain's constants, and a
    renderer that imported them back would recreate the coupling this split
    removes."""
    issues, report, lint_report = payload["issues"], payload["report"], payload["lint"]
    out = [f"== mb curator — {report['notes']} notes =="]
    for key, label in (*CURATOR_SECTIONS,
                       ("orphan_source", f"Sources unprocessed >{orphan_source_days}d")):
        items = issues[key]
        state = "FAIL" if items and key == "broken_links" else "WARN" if items else "PASS"
        out.append(f"\n[{state}] {label}: {len(items)}")
        out += [f"   - {it}" for it in items[:10]]

    out.append(f"\nOrphans (no links in/out): {len(report['orphans'])}")
    out += [f"   - {rel}" for rel in report["orphans"][:10]]
    out.append(f"Stale active notes (>{stale_days}d): {len(report['stale'])}")
    out += [f"   - {rel}" for rel in report["stale"][:10]]
    if report.get("usage_tracked_notes"):
        unused = report.get("stale_unused") or []
        out.append(f"  of those, never opened in recorded history: {len(unused)} "
                   f"(usage known for {report['usage_tracked_notes']} notes)")
        out += [f"   - {rel}" for rel in unused[:10]]

    out.append(f"\nLink suggestions (cosine>{suggest_threshold}, no existing link) — "
               f"NOT applied, approve first:")
    if not report["link_suggestions"]:
        out.append("   (none)")
    out += [f"   {sug['score']:.2f}  {sug['a']}  <->  {sug['b']}"
            for sug in report["link_suggestions"]]

    dup = lint_report["duplicates"]
    out.append(f"\nDuplicate sections (same file, cosine>{duplicate_threshold}): "
               f"{len(dup['same_file'])}")
    out += [f"   {d['score']:.2f}  {d['a']['file']} :: {d['a']['section']}  <->  "
            f"{d['b']['section']}" for d in dup["same_file"][:10]]
    out.append(f"Duplicate sections (cross file, cosine>{duplicate_threshold}): "
               f"{len(dup['cross_file'])}")
    out += [f"   {d['score']:.2f}  {d['a']['file']}::{d['a']['section']}  <->  "
            f"{d['b']['file']}::{d['b']['section']}" for d in dup["cross_file"][:10]]
    out.append(f"Supersession candidates (gap>={supersession_gap_days}d): "
               f"{len(lint_report['supersession'])}")
    out += [f"   {sup['older']} -> possibly superseded by {sup['newer']} "
            f"(gap {sup['gap_days']}d)" for sup in lint_report["supersession"][:10]]
    out.append(f"Context budget/reference issues: {len(lint_report['context_issues'])}")
    for c in lint_report["context_issues"][:10]:
        where = f"{c['project']}/{c['module']}" if c.get("module") else c["project"]
        out.append(f"   [{c['kind']}] {where}: {c['note']}")
    return "\n".join(out)


def _code_graph_line(g: dict[str, Any]) -> str | None:
    """Shown when the graph is on or graphs are on disk; otherwise no line,
    like the code corpus: a switch never turned on leaves no empty line."""
    if not g.get("enabled") and not g.get("units"):
        return None
    if not g.get("units"):
        return "code graph    : on, nothing built — run `mb code build`"
    broken = sum(1 for u in g["units"] if "error" in u)
    return (f"code graph    : {len(g['units'])} graphs, {g['files']} files, {g['nodes']} nodes, "
            f"{g['edges']} edges, {g['db_bytes'] / 1e6:.1f} MB"
            + (f", {broken} unreadable (mb doctor)" if broken else "")
            + ("" if g.get("enabled") else " — switched off, still on disk"))


def stats(s: dict[str, Any]) -> str:
    graph = _code_graph_line(s.get("code_graph") or {})
    if not s.get("present"):
        return (f"index: absent — {s.get('vault_notes', 0)} vault notes, "
                f"{s.get('unindexed', 0)} unindexed") + (f"\n{graph}" if graph else "")
    out = [f"notes indexed : {s['notes']} / {s['vault_notes']} vault "
           f"({s['unindexed']} unindexed)",
           f"chunks        : {s['chunks']} ({s['avg_chunks_per_note']}/note, "
           f"{s['duplicate_chunks']} duplicate)",
           f"text indexed  : {s['chars_indexed']} chars",
           "by type       : " + ", ".join(f"{k}={v}" for k, v in s["by_type"].items()),
           f"chunk schema  : {s['chunk_schema']}"]
    src = s.get("sources") or {}
    if src.get("documents"):
        out.append(f"sources       : {src['documents']} documents "
                   f"({', '.join(f'{k}={v}' for k, v in src['by_status'].items())}), "
                   f"{src['chunks']} chunks, {src['derived_notes']} derived notes")
    code = s.get("code") or {}
    if code.get("configured"):
        # shown only when the corpus was switched on: a user who never set
        # code_repos_dir should not have a permanent empty line about it
        if code.get("present"):
            out.append(f"code          : {len(code['repos'])} repos, {code['files']} files, "
                       f"{code['chunks']} chunks, built {code['indexed_at']}")
            # a chunk with no repo lands in the "-" bucket and would otherwise
            # just inflate the repo count — it reads as one extra repo instead
            # of as the bug it is
            if "-" in (code.get("repos") or {}):
                out.append(f"                {code['repos']['-']} chunk(s) with NO repo "
                           f"— report this, every code chunk should have one")
            if code.get("excluded"):
                out.append("                excluded as vendored: "
                           + ", ".join(code["excluded"]))
        else:
            out.append("code          : configured but not built — "
                       "run `mb index rebuild --code`")
    if graph:
        out.append(graph)
    return "\n".join(out)


def gaps(result: dict[str, Any], heuristic: bool = False) -> str:
    out = [f"== mb curator gaps — {result['total']} open items "
           f"over {result['notes']} notes ==",
           "  " + "  ".join(f"{name}:{count}"
                            for name, count in sorted(result["counts"].items()))]
    if not result["items"]:
        out.append("\n(nothing outstanding)")
    for entry in result["items"]:
        detail = entry["detail"]
        extra = detail.get("reason") or detail.get("where") or ""
        if detail.get("inbound"):
            extra = f"linked from {detail['inbound']} notes"
        if detail.get("heuristic"):
            extra = f"HEURISTIC — appears in {detail.get('document_frequency')} notes"
        out.append(f"  {entry['score']:5.2f}  [{entry['signal']}] {entry['key']}"
                   + (f"  — {extra}" if extra else "")
                   + (f"  ({entry['age_days']:.0f}d)" if entry["age_days"] else ""))
    if result["truncated"]:
        out.append("\nnot shown (per-signal ceiling): "
                   + ", ".join(f"{name} +{count}"
                               for name, count in sorted(result["truncated"].items()))
                   + "  — raise with --per-signal N")
    if not heuristic:
        out.append("(--heuristic adds frequency-based term guesses, off by default)")
    return "\n".join(out)


def journey(payload: dict[str, Any], *, limit: int = 60) -> str:
    """One dated list of what the vault gained. Grouped by day because that is
    how the question is asked — "what came out of last week" — not by folder."""
    scope = f" project={payload['project']}" if payload.get("project") else ""
    out = [f"== mb journey — {payload['from']} .. {payload['to']} "
           f"({payload['days']}d{scope}) =="]
    kinds = ", ".join(f"{k} {n}" for k, n in payload["by_kind"].items()) or "nothing"
    out.append(f"  {payload['total']} entries: {kinds}")
    if payload.get("learning_tasks"):
        out.append(f"  learning entries span {payload['learning_tasks']} task(s)")
    if not payload["entries"]:
        out.append("\n(nothing in this window)")
        return "\n".join(out)

    shown = payload["entries"][:limit]
    current = None
    for e in shown:
        if e["day"] != current:
            current = e["day"]
            out.append(f"\n{current}")
        label = e.get("label")
        tail = f" — {label}" if label else ""
        out.append(f"  [{e['kind']:<9}] {e['title']}{tail}")
    hidden = len(payload["entries"]) - len(shown)
    if hidden > 0:
        out.append(f"\n... {hidden} more (raise --limit, or narrow with --days/--project)")
    return "\n".join(out)


# --- token usage -------------------------------------------------------------

def _tok(n: int | None) -> str:
    if n is None:
        return "-"
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _usd(v: float | None) -> str:
    return "-" if v is None else (f"${v:.2f}" if abs(v) >= 0.01 else f"${v:.4f}")


def usage(p: dict[str, Any], *, turns: bool = False, limit: int = 12) -> str:
    """Two columns for every step, because either one alone misleads: what the
    turn was BILLED, and what it ADDED to the context and left there."""
    if p.get("error"):
        return f"mb usage: {p['error']} ({p.get('path') or p.get('root', '')})"

    t, ctx = p["totals"], p["context"]
    models = ", ".join(p["models"]) or "unknown model"
    out = [f"== mb usage — session {p['session'][:8]} ({models}) ==",
           f"  {t['turns']} turns · out {_tok(t['output'])} · cache read "
           f"{_tok(t['cache_read'])} · cache write {_tok(t['cache_write'])} · "
           f"fresh in {_tok(t['input'])} · {_usd(t['cost_usd'])}"]
    if p.get("subagent_cost_usd") is not None:
        out.append(f"  + {len(p['subagents'])} subagent(s): "
                   f"{_usd(p['subagent_cost_usd'])} — spend the parent's own "
                   f"usage never shows")

    out.append("")
    out.append(f"context: {_tok(ctx['start'])} at start → peak {_tok(ctx['peak'])} "
               f"→ {_tok(ctx['end'])} at end, {ctx['resets']} reset(s)")
    if ctx.get("baseline_carry_usd") is not None:
        out.append(f"  the starting prefix alone (system prompt, tool schemas, "
                   f"CLAUDE.md, memory) was re-read on every later turn: "
                   f"{_usd(ctx['baseline_carry_usd'])}")

    if p.get("by_source"):
        out.append("")
        out.append(f"where the context came from — {_tok(ctx['grew'])} added"
                   + (f", {_tok(ctx['reclaimed'])} reclaimed by resets"
                      if ctx.get("reclaimed") else "")
                   + "  (carry $ = cost of re-reading it on later turns)")
        for row in p["by_source"]:
            share = f"{row['share'] * 100:4.1f}%" if row["share"] is not None else "   - "
            out.append(f"  {row['label'][:44]:<46}{_tok(row['added']):>8}"
                       f"{share:>7}{_usd(row['carry_usd']):>10}")

    if p["by_tool"]:
        out.append("")
        out.append("what grew the context, by tool  "
                   "(added = estimated share; carried = re-read on later turns)")
        out.append(f"  {'tool':<22}{'calls':>6}{'returned':>10}{'added':>9}"
                   f"{'carried':>10}{'carry $':>10}  heaviest call")
        for row in p["by_tool"]:
            heaviest = row.get("heaviest") or ""
            errs = f" ({row['errors']} err)" if row["errors"] else ""
            # `returned` is measured characters, not tokens — it is what the
            # tool handed back, before any of it was priced.
            out.append(f"  {row['name'][:21]:<22}{row['calls']:>6}"
                       f"{_tok(row['result_chars']):>9}c{_tok(row['added']):>9}"
                       f"{_tok(row['carry_tokens']):>10}"
                       f"{_usd(row['carry_usd']):>10}  {heaviest[:38]}{errs}")

    if p["heaviest_steps"]:
        out.append("")
        out.append("heaviest steps by context added")
        for step in p["heaviest_steps"][:limit]:
            tools = ", ".join(step["tools"][:4]) or "no tool call"
            out.append(f"  #{step['index']:<4} +{_tok(step['added_to_context']):<8}"
                       f"prompt {_tok(step['prompt_size']):<8}"
                       f"{_usd(step['cost_usd']):>9}  {tools}")

    if p["costly_steps"]:
        out.append("")
        out.append("most expensive turns")
        for step in p["costly_steps"][:limit]:
            tools = ", ".join(step["tools"][:4]) or "no tool call"
            out.append(f"  #{step['index']:<4} {_usd(step['cost_usd']):<9}"
                       f"prompt {_tok(step['prompt_size']):<8}"
                       f"out {_tok(step['output']):<7}  {tools}")

    if p["subagents"]:
        out.append("")
        out.append("subagents")
        for a in p["subagents"]:
            kind = f" {a['kind'][:34]}" if a["kind"] else ""
            out.append(f"  {a['agent_id'][:12]:<14}{a['turns']:>4} turns  "
                       f"peak {_tok(a['peak_prompt']):<8}out {_tok(a['output']):<8}"
                       f"{_usd(a['cost_usd']):>9} {kind}")

    if turns:
        out.append("")
        out.append("every turn")
        for step in p["turns"]:
            mark = " RESET" if step["context_reset"] else ""
            tools = ", ".join(step["tools"][:3])
            out.append(f"  #{step['index']:<4}{_usd(step['cost_usd']):>9}  "
                       f"prompt {_tok(step['prompt_size']):<8}"
                       f"out {_tok(step['output']):<7}"
                       f"Δctx {_tok(step['added_to_context']):<8}{tools}{mark}")

    for w in p["warnings"]:
        out.append(f"\nnote: {w}")
    return "\n".join(out)


def usage_brief(p: dict[str, Any]) -> str:
    """One line, deliberately — this is printed while a phase closes, so the
    report must not cost more than the step it measures."""
    if p.get("error"):
        return f"mb usage: {p['error']}"
    return (f"{p['session'][:8]} turns {p['from_turn']}-{p['next_turn'] - 1} "
            f"({p['turns']}) · {_tok(p['tokens'])} tokens · out {_tok(p['output'])} "
            f"· {_usd(p['cost_usd'])} · next --from {p['next_turn']}")


def usage_overview(p: dict[str, Any]) -> str:
    """Which session to look inside, before looking inside one."""
    rows = p["sessions"]
    scanned = p.get("scanned", len(rows))
    # The total covers the whole window; the table may be shorter. Saying so is
    # the difference between "this is the spend" and "this is some of it".
    shown = f", showing top {len(rows)} of {scanned}" if scanned > len(rows) else ""
    out = [f"== mb usage — last {p['days']}d, {scanned} session(s){shown}, "
           f"{_usd(p['total_usd'])} =="]
    if not rows:
        out.append("  (no transcripts in this window)")
        return "\n".join(out)
    out.append(f"  {'session':<10}{'turns':>6}{'output':>9}{'peak ctx':>10}"
               f"{'agents':>8}{'cost':>10}  project")
    for r in rows:
        out.append(f"  {r['session'][:8]:<10}{r['turns']:>6}{_tok(r['output']):>9}"
                   f"{_tok(r['peak_context']):>10}{r['subagents']:>8}"
                   f"{_usd(r['cost_usd']):>10}  {r['project'][:34]}")
    for w in p.get("warnings", []):
        out.append(f"\nnote: {w}")
    out.append("\nlook inside one with: mb usage --session <id>")
    return "\n".join(out)
