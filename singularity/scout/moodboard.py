"""HTML contact-sheet moodboard for owner acceptance (WSF-09 DoD).
Self-contained (inline CSS, no scripts), deterministic ordering (§8).
All untrusted strings are HTML-escaped; embeds are plain <img>/<a>."""
from __future__ import annotations

import html


def _badge(rec: dict) -> str:
    policy = rec["license_policy"]
    lic = html.escape(rec.get("license") or "unknown")
    color = {"api-free-license": "#2e7d32", "api-hotlink": "#1565c0",
             "links-only": "#6d4c41", "per-block": "#7b1fa2"}.get(policy, "#555")
    return (f'<span class="badge" style="background:{color}" '
            f'title="{html.escape(policy)}">{lic}</span>')


def _card(rec: dict) -> str:
    title = html.escape(rec.get("title") or rec["page_url"])
    page = html.escape(rec["page_url"], quote=True)
    creator = html.escape(rec.get("creator") or "")
    img_src = rec.get("local_thumb") or rec.get("media_url")
    img = (f'<img loading="lazy" src="{html.escape(img_src, quote=True)}" alt="">'
           if img_src else '<div class="noimg">no preview — link only</div>')
    meta = f'<span class="src">{html.escape(rec["source"])}</span> {_badge(rec)}'
    who = f'<div class="creator">{creator}</div>' if creator else ""
    query = html.escape(rec.get("query") or "")
    return (f'<figure class="card"><a href="{page}" target="_blank" rel="noopener">'
            f'{img}</a><figcaption><a href="{page}" target="_blank" rel="noopener">'
            f'{title}</a>{who}<div class="meta">{meta} '
            f'<span class="q">{query}</span></div></figcaption></figure>')


def render(manifest: dict) -> str:
    run = manifest["run"]
    queries = ", ".join(html.escape(q) for q in run["queries"])
    src_rows = "".join(
        f'<tr><td>{html.escape(name)}</td><td>{html.escape(info["status"])}</td>'
        f'<td>{info.get("results", "")}</td>'
        f'<td class="reason">{html.escape(str(info.get("reason") or ""))}</td></tr>'
        for name, info in manifest["sources"].items())
    by_source: dict[str, list] = {}
    for rec in manifest["records"]:
        by_source.setdefault(rec["source"], []).append(rec)
    sections = "".join(
        f'<h2>{html.escape(source)} <small>({len(records)})</small></h2>'
        f'<div class="grid">{"".join(_card(r) for r in records)}</div>'
        for source, records in by_source.items())
    log = run["budget_log"]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>design-scout moodboard</title>
<style>
 body{{background:#111;color:#ddd;font:14px/1.5 system-ui,sans-serif;margin:2rem}}
 h1{{font-size:1.3rem}} h2{{margin-top:2rem;border-bottom:1px solid #333}}
 table{{border-collapse:collapse;margin:1rem 0}} td,th{{border:1px solid #333;padding:.3rem .6rem}}
 .reason{{color:#999;max-width:40rem}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:1rem}}
 .card{{margin:0;background:#1a1a1a;border:1px solid #2a2a2a;border-radius:8px;overflow:hidden}}
 .card img{{width:100%;height:160px;object-fit:cover;display:block;background:#000}}
 .noimg{{height:160px;display:flex;align-items:center;justify-content:center;color:#666}}
 figcaption{{padding:.5rem .7rem}} figcaption a{{color:#9ecbff;text-decoration:none}}
 .creator{{color:#999;font-size:.85em}}
 .meta{{margin-top:.3rem;display:flex;gap:.4rem;flex-wrap:wrap;align-items:center}}
 .src{{color:#bbb;font-size:.8em}} .q{{color:#777;font-size:.75em}}
 .badge{{color:#fff;font-size:.7em;padding:.1rem .45rem;border-radius:99px}}
 footer{{margin-top:3rem;color:#777;font-size:.85em}}
</style></head><body>
<h1>design-scout moodboard</h1>
<p>queries: <strong>{queries}</strong> · retrieved {html.escape(run["retrieved_at"])}
 · records {len(manifest["records"])} · dedup dropped {manifest["dedup_dropped"]}</p>
<table><tr><th>source</th><th>status</th><th>results</th><th>note</th></tr>{src_rows}</table>
{sections}
<footer>budget log: fetches {log["fetches"]}, retries {log["retries"]},
 thumbs {log["thumb_downloads"]}, elapsed {log["elapsed_s"]}s,
 stop: {html.escape(str(log["stop_condition"]))}.
 License policies per CONTRACT.md §4 (OD-2): gallery entries are links/embeds only,
 downloads exist only for api-free-license sources. Owner acceptance gate: this board
 is an INPUT for design, not an approval.</footer>
</body></html>"""
