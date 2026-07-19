"""HTML review board for pattern candidates (WSF-E02). Self-contained, no
scripts, untrusted strings escaped; scores expandable per component. The
board is an INPUT for the owner's shortlist gate, not an approval."""
from __future__ import annotations

import html


def _chip(text: str, color: str = "#333") -> str:
    return (f'<span class="chip" style="background:{color}">'
            f'{html.escape(str(text))}</span>')


def _card(cand: dict, score: dict) -> str:
    name = html.escape(cand.get("name") or cand["candidate_id"])
    desc = html.escape((cand.get("description") or "")[:280])
    src = html.escape(cand.get("source_url") or "", quote=True)
    preview = cand.get("preview_url")
    img = (f'<a href="{src}" target="_blank" rel="noopener">'
           f'<img loading="lazy" src="{html.escape(preview, quote=True)}" alt=""></a>'
           if preview else '<div class="noimg">no preview</div>')
    caps = " ".join(_chip(c, "#1b4332") for c in cand.get("capabilities", []))
    lic_color = "#2e7d32" if cand.get("importable") else "#7a1f1f"
    lic = _chip(cand.get("license") or "unknown", lic_color)
    stale = _chip("STALE CACHE", "#8a6d00") if cand.get("stale") else ""
    fails = cand.get("_hard_fails") or []
    fail_html = "".join(f'<div class="fail">✕ {html.escape(f)}</div>' for f in fails)
    install = cand.get("install_command")
    install_html = (f'<code class="install">{html.escape(install)}</code>'
                    if install else "")
    deps = ", ".join(html.escape(d) for d in cand.get("dependencies", [])) or "—"
    comps = "".join(
        f'<tr><td>{html.escape(k)}</td><td>{v["points"]}/{v["max"]}</td>'
        f'<td class="why">{html.escape(v["why"])}</td></tr>'
        for k, v in score["components"].items())
    return f"""<article class="card">
{img}
<div class="body">
<h3><a href="{src}" target="_blank" rel="noopener">{name}</a>
 <span class="score">{score["total"]}</span></h3>
<p>{desc}</p>
<div class="meta">{lic} {stale} {caps}</div>
<div class="deps">deps: {deps}</div>
{fail_html}{install_html}
<details><summary>score evidence</summary>
<table>{comps}</table></details>
</div></article>"""


def render(payload: dict, scored: list[tuple[dict, dict]]) -> str:
    queries = ", ".join(html.escape(q) for q in payload.get("queries", []))
    caps = ", ".join(html.escape(c) for c in payload.get("capabilities", []))
    prov_rows = "".join(
        f'<tr><td>{html.escape(n)}</td><td>{html.escape(i.get("status", ""))}</td>'
        f'<td>{i.get("results", "")}</td></tr>'
        for n, i in payload.get("providers", {}).items())
    cards = "".join(_card(c, s) for c, s in scored)
    importable = sum(1 for c, _ in scored if c.get("importable"))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pattern-scout review board</title>
<style>
 body{{background:#101014;color:#ddd;font:14px/1.5 system-ui,sans-serif;margin:2rem}}
 h1{{font-size:1.3rem}} table{{border-collapse:collapse}}
 td,th{{border:1px solid #333;padding:.25rem .55rem}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:1rem;margin-top:1.5rem}}
 .card{{background:#17171c;border:1px solid #2a2a30;border-radius:8px;overflow:hidden}}
 .card img{{width:100%;height:170px;object-fit:cover;display:block;background:#000}}
 .noimg{{height:170px;display:flex;align-items:center;justify-content:center;color:#555}}
 .body{{padding: .6rem .8rem}} .body a{{color:#9ecbff;text-decoration:none}}
 .score{{float:right;background:#233;border-radius:6px;padding:.05rem .5rem;font-weight:600}}
 .chip{{color:#fff;font-size:.7em;padding:.1rem .5rem;border-radius:99px;margin-right:.25rem}}
 .deps{{color:#888;font-size:.8em;margin-top:.3rem}}
 .fail{{color:#ff8f8f;font-size:.8em;margin-top:.3rem}}
 .install{{display:block;background:#0c0c10;border:1px solid #2a2a30;padding:.35rem .5rem;
           margin-top:.4rem;font-size:.72em;overflow-x:auto;white-space:nowrap}}
 details{{margin-top:.45rem;font-size:.8em}} .why{{color:#999}}
 footer{{margin-top:2.5rem;color:#777;font-size:.85em}}
</style></head><body>
<h1>pattern-scout review board</h1>
<p>queries: <strong>{queries}</strong> · capabilities: {caps or "—"} ·
 candidates {len(scored)} · importable {importable}</p>
<table><tr><th>provider</th><th>status</th><th>results</th></tr>{prov_rows}</table>
<div class="grid">{cards}</div>
<footer>Score forms the shortlist only — the final visual choice is a HUMAN
gate (decision §5). Hard-fail marks (✕) block import per contract §2;
license=unknown is never importable. No candidate code was executed to build
this board.</footer>
</body></html>"""
