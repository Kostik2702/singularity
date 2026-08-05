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


def stats(s: dict[str, Any]) -> str:
    if not s.get("present"):
        return (f"index: absent — {s.get('vault_notes', 0)} vault notes, "
                f"{s.get('unindexed', 0)} unindexed")
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
