"""Provider adapters + search runner (CONTRACT.md §4-§6).

Adapters never raise: fetch failures come back as typed FetchResult classes,
payload-shape failures are caught by the runner's isolation wrapper
(unavailable:adapter-error). Every returned record passes candidate.validate
by construction (make_candidate)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from urllib.parse import quote_plus

from . import cache as cache_mod
from .candidate import CandidateError, import_gate, make_candidate, validate

# --- capability inference ----------------------------------------------------

_CAP_KEYWORDS = {
    "particles": ("particle", "points", "rain", "snow", "confetti", "sprites"),
    "shader": ("shader", "glsl", "tsl", "postprocessing", "bloom", "glitch"),
    "scroll-linked": ("scroll", "scrolltrigger", "parallax", "scrub"),
    "text": ("text", "typing", "typewriter", "font", "ascii", "letter"),
    "camera-path": ("camera", "flythrough", "orbit", "dolly"),
    "transition": ("transition", "reveal", "fade", "morph", "stagger"),
    "interaction": ("hover", "drag", "gesture", "click", "interactive", "mouse"),
    "media": ("video", "audio", "image", "gallery", "carousel"),
    "background": ("background", "backdrop", "matrix", "terminal", "grid",
                   "aurora", "gradient", "noise"),
    "cursor": ("cursor", "pointer", "trail"),
    "3d": ("3d", "three", "webgl", "webgpu", "scene", "mesh", "geometry"),
    "hero": ("hero", "landing", "headline"),
}


def infer_capabilities(*texts: str) -> list[str]:
    blob = " ".join(t.lower() for t in texts if t)
    return [cap for cap, words in _CAP_KEYWORDS.items()
            if any(w in blob for w in words)]


def _match_score(item_text: str, queries: list[str]) -> float:
    """Relevance score in [0, 1]. Short tokens are load-bearing ('3d', 'ui') —
    only single-char tokens are dropped (review B: len>2 returned zero for
    '3d'). A query matches when at most one token misses; partial matches
    score lower, and adapters SORT by score before the per-provider cap, so
    broad hits never displace exact ones (live regression 2026-07-19: strict
    all-tokens AND zeroed react-bits/three-examples for 2-token queries)."""
    blob = item_text.lower()
    best = 0.0
    for query in queries:
        tokens = [t for t in re.split(r"\W+", query.lower()) if len(t) >= 2]
        if not tokens:
            continue
        hits = sum(t in blob for t in tokens)
        if hits == 0 or hits < len(tokens) - 1:
            continue
        best = max(best, hits / len(tokens))
    return best


def _matches(item_text: str, queries: list[str]) -> bool:
    return _match_score(item_text, queries) > 0


# --- adapters ---------------------------------------------------------------
# signature: adapter(prov, queries, capabilities, ctx) -> (candidates, status)
# ctx: {"fetch": Fetcher, "budgets": dict}


def adapt_twentyfirst_cli(prov, queries, capabilities, ctx):
    exe = shutil.which(prov.get("cli_command", "21st") + ".cmd") \
        or shutil.which(prov.get("cli_command", "21st"))
    if not exe:
        return [], "unavailable:cli-missing"
    timeout = float(ctx["budgets"].get("cli_timeout_s", 90))
    per = int(ctx["budgets"].get("max_candidates_per_provider", 20))
    out, status = [], None
    for query in queries:
        # BatBadBut class (CVE-2024-1874): .cmd shims re-parse argv on older
        # interpreters — refuse shell metacharacters instead of trusting them
        if re.search(r'[&|<>^%"]', query):
            status = status or "unavailable:cli-error"
            continue
        cmd = [exe, "search", query, "--limit", str(min(per, 10)), "--json"]
        try:
            # no console window per child (singularity.platform.proc.NO_WINDOW; the
            # factory imports nothing from the engine, so the expression is repeated)
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", timeout=timeout,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (subprocess.TimeoutExpired, OSError):
            status = status or "unavailable:timeout"
            continue
        if proc.returncode != 0:
            status = status or "unavailable:cli-error"
            continue
        try:
            items = json.loads(proc.stdout)
        except ValueError:
            status = status or "unavailable:bad-json"
            continue
        status = "live"
        for item in items:
            if item.get("id") in (None, ""):  # id-less items would collide (review F)
                continue
            caps = infer_capabilities(item.get("name", ""),
                                      item.get("description", ""), query)
            out.append(make_candidate(
                prov["name"], str(item.get("id")),
                kind="component" if item.get("type") == "component" else "template",
                name=item.get("name", ""),
                description=item.get("description", ""),
                source_url=item.get("url") or "https://21st.dev/",
                preview_url=item.get("previewUrl"),
                license_str="unknown",  # 21st search output carries no license
                price_tier="free",
                framework="react",
                install_method="shadcn",
                install_command=item.get("installCommand"),
                capabilities=caps,
                evidence=[f"query: {query}", f"author: {item.get('author')}"]))
    return out, status or "unavailable:other"


def adapt_shadcn_registry(prov, queries, capabilities, ctx):
    res = ctx["fetch"](prov["registry_url"])
    if not res.ok:
        return [], f"unavailable:{res.error_class}"
    try:
        payload = json.loads(res.body.decode("utf-8", errors="replace"))
    except ValueError:
        return [], "unavailable:bad-json"
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return [], "unavailable:bad-json"
    scored = []
    for item in items:
        text = " ".join(str(item.get(k, "")) for k in ("name", "title", "description"))
        score = _match_score(text, queries)
        if score > 0:
            scored.append((score, len(scored), item))
    scored.sort(key=lambda t: (-t[0], t[1]))  # best match first, stable
    out = []
    for _, _, item in scored:
        name = item.get("name", "")
        item_url = prov.get("item_url_template", "{name}").format(name=quote_plus(name))
        caps = infer_capabilities(text, " ".join(item.get("dependencies") or []))
        out.append(make_candidate(
            prov["name"], name,
            kind="component",
            name=item.get("title") or name,
            description=item.get("description", ""),
            source_url=item_url,
            license_str=prov.get("license", "unknown"),
            usage_rights=prov.get("usage_rights", "unknown"),
            framework=prov.get("framework", "react"),
            install_method="shadcn",
            install_command=f"npx shadcn@latest add {item_url}",
            dependencies=item.get("dependencies") or [],
            capabilities=caps,
            risks=list(prov.get("risks") or []),
            evidence=[f"registry: {prov['registry_url']}"]))
    return out, "live"


def adapt_three_examples(prov, queries, capabilities, ctx):
    res = ctx["fetch"](prov["catalog_url"])
    if not res.ok:
        return [], f"unavailable:{res.error_class}"
    try:
        catalog = json.loads(res.body.decode("utf-8", errors="replace"))
    except ValueError:
        return [], "unavailable:bad-json"
    if not isinstance(catalog, dict):
        return [], "unavailable:bad-json"
    scored = []
    for category, names in catalog.items():
        if not isinstance(names, list):
            continue
        for name in names:
            text = f"{category} {str(name).replace('_', ' ')}"
            score = _match_score(text, queries)
            if score > 0:
                scored.append((score, len(scored), category, name))
    scored.sort(key=lambda t: (-t[0], t[1]))
    out = []
    for _, _, category, name in scored:
        text = f"{category} {str(name).replace('_', ' ')}"
        caps = infer_capabilities(text) + ["3d"]
        out.append(make_candidate(
            prov["name"], str(name),
            kind="example",
            name=str(name),
            description=f"official three.js example ({category})",
            source_url=prov["source_url_template"].format(name=name),
            preview_url=prov.get("example_url_template", "").format(name=name) or None,
            license_str=prov.get("license", "MIT"),
            usage_rights=prov.get("usage_rights", "commercial"),
            framework=prov.get("framework", "three"),
            install_method="source",
            capabilities=sorted(set(caps)),
            evidence=[f"catalog category: {category}"]))
    return out, "live"


def adapt_reference_only(prov, queries, capabilities, ctx):
    out = []
    wanted = set(capabilities)
    for link in prov.get("links", []):
        link_caps = set(link.get("capabilities") or [])
        if wanted and not (wanted & link_caps) \
                and not _matches(link.get("name", ""), queries):
            continue
        out.append(make_candidate(
            prov["name"], re.sub(r"\W+", "-", link["name"].lower()).strip("-"),
            kind="motion-recipe",
            name=link["name"],
            description=prov.get("notes", ""),
            source_url=link["url"],
            license_str="custom",
            install_method="reference-only",
            capabilities=sorted(link_caps),
            evidence=["curated reference link (inspiration lane)"]))
    return out, "live"


ADAPTERS = {
    "twentyfirst_cli": adapt_twentyfirst_cli,
    "shadcn_registry": adapt_shadcn_registry,
    "three_examples": adapt_three_examples,
    "reference_only": adapt_reference_only,
}


# --- runner -----------------------------------------------------------------

def run_search(registry: dict, queries: list[str], capabilities: list[str],
               fetch, log, use_cache: bool = True,
               adapters: dict | None = None) -> dict:
    budgets = registry["budgets"]
    per = int(budgets.get("max_candidates_per_provider", 20))
    max_providers = int(budgets.get("max_providers_per_run", 8))
    adapters = adapters or ADAPTERS
    ctx = {"fetch": fetch, "budgets": budgets}
    statuses: dict[str, dict] = {}
    candidates: list[dict] = []
    used = 0
    for prov in registry["providers"]:
        name = prov["name"]
        if prov.get("enabled") is False:
            statuses[name] = {"status": "skipped:disabled",
                              "reason": prov.get("disabled_reason")}
            continue
        if used >= max_providers or log.stop_condition:
            statuses[name] = {"status": "skipped:budget"}
            continue
        used += 1
        adapter = adapters.get(prov["adapter"])
        if adapter is None:
            statuses[name] = {"status": "unavailable:adapter-error",
                              "reason": f"unknown adapter {prov['adapter']}"}
            continue
        key = cache_mod.cache_key(name, queries, capabilities)
        reason = None
        try:
            found, status = adapter(prov, queries, capabilities, ctx)
        except Exception as exc:  # noqa: BLE001 — §4: isolation wrapper
            found, status = [], "unavailable:adapter-error"
            reason = f"{type(exc).__name__}: {exc}"[:200]
        if status == "unavailable:budget-stop":
            status = "skipped:budget"

        # §2 trust boundary is HERE, not inside adapters (review D): every
        # record is re-gated + validated; violations are dropped and counted —
        # a hand-built dict with importable=true can never pass through.
        vetted: list[dict] = []
        dropped_invalid = 0
        seen_ids: set[str] = set()
        dedup_merged = 0
        for rec in found:
            try:
                if not isinstance(rec, dict):
                    raise CandidateError("candidate is not a dict")
                rec["importable"], rec["import_block_reason"] = import_gate(rec)
                validate(rec)
            except CandidateError:
                dropped_invalid += 1
                continue
            if rec["candidate_id"] in seen_ids:  # review C: per-query dupes
                dedup_merged += 1
                continue
            seen_ids.add(rec["candidate_id"])
            vetted.append(rec)
        found = vetted[:per]

        if status == "live":
            statuses[name] = {"status": "live", "results": len(found)}
            if dropped_invalid:
                statuses[name]["dropped_invalid"] = dropped_invalid
            if dedup_merged:
                statuses[name]["dedup_merged"] = dedup_merged
            candidates.extend(found)
            # §5: writes ALWAYS happen (use_cache only bypasses reads) and an
            # empty live result never clobbers last-known-good (review A/erosion)
            if found:
                cache_mod.store(key, {"provider": name, "queries": queries,
                                      "capabilities": capabilities,
                                      "candidates": found})
        else:
            entry = cache_mod.load(key) if use_cache else None
            if entry:
                stale = cache_mod.serve_stale(entry)[:per]
                statuses[name] = {"status": f"{status}+cache",
                                  "results": len(stale), "stale": True}
                candidates.extend(stale)
            else:
                statuses[name] = {"status": status, "results": 0}
            if reason:
                statuses[name]["reason"] = reason
    return {"providers": statuses, "candidates": candidates,
            "budget_log": log.as_dict()}
