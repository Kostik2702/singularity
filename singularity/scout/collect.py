"""Collector core for design-scout: per-source adapters -> normalized records
with provenance, dedup, license-policy enforcement by construction (§4) and
thumbnail downloads only for api-free-license sources."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit, quote_plus

from ._contract import atomic_write_bytes
from .fetch import BudgetLog, FetchResult
from .registry import auth_headers, source_key


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_url(url: str) -> str:
    """Dedup key (§8): lowercased host, stripped query/fragment/trailing slash."""
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def record_id(source: str, page_url: str) -> str:
    digest = hashlib.sha1(normalize_url(page_url).encode("utf-8")).hexdigest()[:12]
    return f"{source}:{digest}"


def safe_url(url: str | None) -> str | None:
    """Scheme allowlist for untrusted URLs (review F2): only http(s) may reach
    the moodboard — a javascript:/data: href from a provider payload would
    execute on owner click. Anything else becomes None."""
    if not url:
        return None
    if urlsplit(url.strip()).scheme.lower() in ("http", "https"):
        return url.strip()
    return None


def make_record(source_cfg: dict, *, query: str, title: str, page_url: str,
                media_url: str | None, license_str: str | None,
                creator: str | None, endpoint: str, http_status: int | None
                ) -> dict | None:
    """None when the record is unusable: no valid http(s) page_url (review F5:
    empty page_urls all hash to the same id and silently collapse in dedup)."""
    page_url = safe_url(page_url)
    if not page_url:
        return None
    media_url = safe_url(media_url)
    return {
        "id": record_id(source_cfg["name"], page_url),
        "source": source_cfg["name"],
        "method": source_cfg["method"],
        "license_policy": source_cfg["license_policy"],
        "query": query,
        "title": (title or "").strip()[:300],
        "page_url": page_url,
        "media_url": media_url,
        "local_thumb": None,
        "license": license_str or "unknown",
        "creator": (creator or "").strip()[:200] or None,
        "retrieved_at": now_iso(),
        "provenance": {"endpoint": endpoint, "http_status": http_status},
    }


class _CardParser(HTMLParser):
    """Generic showcase-grid extractor for links-only HTML sources. Two card
    shapes are recognized (untrusted content is only parsed for these
    attributes — never executed):

    1. <a href><img></a>            (land-book, minimal.gallery)
    2. <article> <img> ... <a href>title</a> </article>  (httpster: img and
       anchors are SIBLINGS inside the card container; the external-host
       anchor wins, its text becomes the title)."""

    def __init__(self, base_url: str):
        super().__init__()
        self.base = base_url
        self.base_host = urlsplit(base_url).netloc.lower()
        self.cards: list[dict] = []
        # pattern 1 state
        self._href: str | None = None
        self._text: list[str] = []
        self._img: str | None = None
        self._alt: str = ""
        # pattern 2 state
        self._article: dict | None = None

    def _img_src(self, attrs: dict) -> str | None:
        src = attrs.get("src") or attrs.get("data-src") or ""
        if src and not src.startswith("data:"):
            return urljoin(self.base, src)
        return None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "article":
            self._article = {"img": None, "alt": "", "anchors": []}
        elif tag == "a" and a.get("href"):
            self._href = urljoin(self.base, a["href"])
            self._text, self._img, self._alt = [], None, ""
        elif tag == "img":
            src = self._img_src(a)
            if not src:
                return
            if self._href:
                self._img, self._alt = src, a.get("alt") or ""
            elif self._article is not None and not self._article["img"]:
                self._article["img"] = src
                self._article["alt"] = a.get("alt") or ""

    def handle_data(self, data):
        if self._href and data.strip():
            self._text.append(data.strip())

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            if self._img:
                title = self._alt or " ".join(self._text)[:120]
                self.cards.append({"href": self._href, "img": self._img,
                                   "title": title})
            elif self._article is not None:
                self._article["anchors"].append(
                    (self._href, " ".join(self._text)[:120]))
            self._href = None
        elif tag == "article" and self._article is not None:
            art = self._article
            self._article = None
            if not art["img"] or not art["anchors"]:
                return
            external = [(h, t) for h, t in art["anchors"]
                        if urlsplit(h).netloc.lower() not in ("", self.base_host)]
            titled = [(h, t) for h, t in art["anchors"] if t]
            href, text = (external or titled or art["anchors"])[0]
            title = next((t for h, t in titled), "") or art["alt"]
            self.cards.append({"href": href, "img": art["img"], "title": title})


def _parse_cards(html: str, base_url: str) -> list[dict]:
    parser = _CardParser(base_url)
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 — broken markup must not kill the run
        pass
    return parser.cards


def _decode(body: bytes) -> str:
    return body.decode("utf-8", errors="replace")


# --- adapters ---------------------------------------------------------------
# Each adapter: (src, queries, fetch, per) -> (records, status_str)
# status: live | unavailable:<class>  (skipped:* is decided by the caller)

def _adapt_openverse(src, queries, fetch, per):
    records = []
    status = None
    per = min(per, int(src.get("max_per", per)))  # anonymous API cap (registry)
    for query in queries:
        url = src["search_endpoint"].format(query=quote_plus(query), per=per)
        res: FetchResult = fetch(url)
        if not res.ok:
            status = status or f"unavailable:{res.error_class}"
            continue
        status = "live"
        try:
            payload = json.loads(_decode(res.body))
        except ValueError:
            status = "unavailable:bad-json"
            continue
        for item in payload.get("results", []):
            lic = item.get("license")
            if not lic:
                continue  # §4: api-free-license item without license is dropped
            lic_str = (lic + " " + (item.get("license_version") or "")).strip()
            records.append(make_record(
                src, query=query, title=item.get("title") or "",
                page_url=item.get("foreign_landing_url") or item.get("url") or "",
                media_url=item.get("thumbnail"), license_str=lic_str,
                creator=item.get("creator"), endpoint=url, http_status=res.status))
    return records, status or "unavailable:other"


def _adapt_arena(src, queries, fetch, per):
    records = []
    status = None
    for query in queries:
        url = src["search_endpoint"].format(query=quote_plus(query))
        res = fetch(url)
        if not res.ok:
            status = status or f"unavailable:{res.error_class}"
            continue
        status = "live"
        try:
            payload = json.loads(_decode(res.body))
        except ValueError:
            status = "unavailable:bad-json"
            continue
        for chan in payload.get("channels", []):
            slug = chan.get("slug")
            owner = (chan.get("user") or {}).get("slug")
            page = (f"https://www.are.na/{owner}/{slug}" if owner and slug
                    else f"https://www.are.na/channels/{slug}")
            records.append(make_record(
                src, query=query, title=chan.get("title") or slug or "",
                page_url=page, media_url=None, license_str=None,
                creator=owner, endpoint=url, http_status=res.status))
            contents_ep = src.get("contents_endpoint")
            if not (slug and contents_ep):
                continue
            c_url = contents_ep.format(slug=slug)
            c_res = fetch(c_url)
            if not c_res.ok:
                continue
            try:
                c_payload = json.loads(_decode(c_res.body))
            except ValueError:
                continue
            for block in c_payload.get("contents", []):
                image = (block.get("image") or {}).get("thumb", {}).get("url")
                if not image:
                    continue
                block_page = (block.get("source") or {}).get("url") \
                    or f"{page}#block-{block.get('id')}"
                records.append(make_record(
                    src, query=query,
                    title=block.get("title") or block.get("generated_title") or "",
                    page_url=block_page, media_url=image,
                    license_str=(block.get("licence") or {}).get("name")
                    if isinstance(block.get("licence"), dict) else None,
                    creator=(block.get("user") or {}).get("slug"),
                    endpoint=c_url, http_status=c_res.status))
    return records, status or "unavailable:other"


def _adapt_generic_html(src, queries, fetch, per):
    """links-only showcase galleries: search endpoint if declared, else
    keyword->path routing, else homepage."""
    base = src.get("base_url") or src.get("probe_url") or ""
    urls: list[tuple[str, str]] = []  # (query, url)
    for query in queries:
        if src.get("search_endpoint") and "{query}" in src["search_endpoint"]:
            urls.append((query, src["search_endpoint"].format(query=quote_plus(query))))
        elif src.get("keyword_paths"):
            matched = [path for kw, path in src["keyword_paths"].items()
                       if kw.lower() in query.lower()]
            for path in matched or []:
                urls.append((query, urljoin(base, path)))
    if not urls:
        first = queries[0] if queries else ""
        urls.append((first, src.get("search_endpoint") or src.get("probe_url") or base))
    seen_urls = set()
    records = []
    status = None
    for query, url in urls:
        if url in seen_urls:
            continue
        seen_urls.add(url)
        res = fetch(url)
        if not res.ok:
            status = status or f"unavailable:{res.error_class}"
            continue
        status = "live"
        for card in _parse_cards(_decode(res.body), url)[:per]:
            records.append(make_record(
                src, query=query, title=card["title"],
                page_url=card["href"], media_url=card["img"],
                license_str=None, creator=None,
                endpoint=url, http_status=res.status))
    return records, status or "unavailable:other"


def _adapt_unsplash(src, queries, fetch, per):
    records = []
    status = None
    headers = auth_headers(src)
    for query in queries:
        url = src["search_endpoint"].format(query=quote_plus(query), per=per)
        res = fetch(url, headers=headers)
        if not res.ok:
            status = status or f"unavailable:{res.error_class}"
            continue
        status = "live"
        try:
            payload = json.loads(_decode(res.body))
        except ValueError:
            status = "unavailable:bad-json"
            continue
        for item in payload.get("results", []):
            records.append(make_record(
                src, query=query,
                title=item.get("description") or item.get("alt_description") or "",
                page_url=(item.get("links") or {}).get("html") or "",
                media_url=(item.get("urls") or {}).get("small"),
                license_str="Unsplash License",
                creator=(item.get("user") or {}).get("name"),
                endpoint=src["search_endpoint"].format(query=quote_plus(query), per=per),
                http_status=res.status))
    return records, status or "unavailable:other"


def _adapt_pexels(src, queries, fetch, per):
    records = []
    status = None
    headers = auth_headers(src)
    for query in queries:
        url = src["search_endpoint"].format(query=quote_plus(query), per=per)
        res = fetch(url, headers=headers)
        if not res.ok:
            status = status or f"unavailable:{res.error_class}"
            continue
        status = "live"
        try:
            payload = json.loads(_decode(res.body))
        except ValueError:
            status = "unavailable:bad-json"
            continue
        for item in payload.get("photos", []):
            records.append(make_record(
                src, query=query, title=item.get("alt") or "",
                page_url=item.get("url") or "",
                media_url=(item.get("src") or {}).get("medium"),
                license_str="Pexels License",
                creator=item.get("photographer"),
                endpoint=url, http_status=res.status))
    return records, status or "unavailable:other"


ADAPTERS = {
    "openverse": _adapt_openverse,
    "arena": _adapt_arena,
    "httpster": _adapt_generic_html,
    "minimal-gallery": _adapt_generic_html,
    "land-book": _adapt_generic_html,
    "godly": _adapt_generic_html,
    "siteinspire": _adapt_generic_html,
    "unsplash": _adapt_unsplash,
    "pexels": _adapt_pexels,
}


# --- probe ------------------------------------------------------------------

def run_probe(registry: dict, transport=None) -> tuple[dict, bool, "BudgetLog"]:
    """Health check every source (evaluative, §2). Injectable transport so the
    floor logic and the FAIL branch are offline-testable (review F3)."""
    from .fetch import Fetcher
    budgets = registry["budgets"]
    log = BudgetLog()
    fetch = Fetcher(budgets, log, transport=transport)
    results: dict[str, dict] = {}
    live_total = live_api = 0
    for src in registry["sources"]:
        name = src["name"]
        if src.get("enabled") is False:
            results[name] = {"status": "skipped:disabled",
                             "reason": src.get("disabled_reason")}
            continue
        if src.get("key_env") and not source_key(src):
            results[name] = {"status": "skipped:no-key",
                             "reason": f"set {src['key_env']} to enable"}
            continue
        res = fetch(src["probe_url"], headers=auth_headers(src))
        if res.ok:
            results[name] = {"status": "live", "http": res.status}
            live_total += 1
            live_api += 1 if src["method"] == "api" else 0
        else:
            results[name] = {"status": f"unavailable:{res.error_class}",
                             "http": res.status}
    floor_total = int(budgets.get("floor_live_total", 3))
    floor_api = int(budgets.get("floor_live_api", 1))
    passed = live_total >= floor_total and live_api >= floor_api
    results["_floor"] = {"live": live_total, "live_api": live_api,
                         "floor_live_total": floor_total,
                         "floor_live_api": floor_api}
    return results, passed, log


# --- run --------------------------------------------------------------------

_EXT_RE = re.compile(r"\.(jpe?g|png|gif|webp|avif)(?:$|\?)", re.IGNORECASE)


def _thumb_ext(url: str) -> str:
    match = _EXT_RE.search(url)
    return ("." + match.group(1).lower().replace("jpeg", "jpg")) if match else ".jpg"


def run_collect(registry: dict, queries: list[str], out_dir: Path,
                fetch, log: BudgetLog) -> dict:
    """Full collect run -> manifest dict (written by the CLI, not here)."""
    budgets = registry["budgets"]
    per = int(budgets.get("max_results_per_source", 24))
    max_sources = int(budgets.get("max_sources_per_run", 8))
    max_thumbs = int(budgets.get("max_thumb_downloads", 60))

    statuses: dict[str, dict] = {}
    all_records: list[dict] = []
    used = 0
    for src in registry["sources"]:
        name = src["name"]
        if src.get("enabled") is False:
            statuses[name] = {"status": "skipped:disabled",
                              "reason": src.get("disabled_reason")}
            continue
        if src.get("key_env") and not source_key(src):
            statuses[name] = {"status": "skipped:no-key",
                              "reason": f"set {src['key_env']} to enable"}
            continue
        if used >= max_sources:
            statuses[name] = {"status": "skipped:budget",
                              "reason": "max_sources_per_run reached"}
            continue
        if log.stop_condition:
            statuses[name] = {"status": "skipped:budget",
                              "reason": log.stop_condition}
            continue
        used += 1
        adapter = ADAPTERS.get(name, _adapt_generic_html)
        try:
            records, status = adapter(src, queries, fetch, per)
        except Exception as exc:  # noqa: BLE001 — review F1: provider schema
            # drift is a dead provider; it must not kill the whole run (§2/§6)
            statuses[name] = {"status": "unavailable:adapter-error",
                              "reason": f"{type(exc).__name__}: {exc}"[:200],
                              "results": 0}
            continue
        if status == "unavailable:budget-stop":  # wall stop is a budget skip (§6)
            status = "skipped:budget"
        records = [r for r in records if r][:per]  # make_record may yield None
        statuses[name] = {"status": status, "results": len(records)}
        all_records.extend(records)

    # §8 dedup: first occurrence wins, registry order preserved.
    seen: set[str] = set()
    dedup_dropped = 0
    records = []
    for rec in all_records:
        key = normalize_url(rec["page_url"]) if rec["page_url"] else rec["id"]
        if key in seen:
            dedup_dropped += 1
            continue
        seen.add(key)
        records.append(rec)

    # §4: thumbnails ONLY for api-free-license, capped by budget.
    thumbs_dir = out_dir / "thumbs"
    for rec in records:
        if rec["license_policy"] != "api-free-license" or not rec["media_url"]:
            continue
        if log.thumb_downloads >= max_thumbs:
            log.stop_condition = log.stop_condition or "max_thumb_downloads"
            break
        res = fetch(rec["media_url"])
        if not res.ok or not res.body:
            continue
        fname = rec["id"].replace(":", "-") + _thumb_ext(rec["media_url"])
        atomic_write_bytes(res.body, thumbs_dir / fname)
        rec["local_thumb"] = f"thumbs/{fname}"
        log.thumb_downloads += 1

    return {
        "schema_version": 1,
        "run": {
            "queries": queries,
            "retrieved_at": now_iso(),
            "budgets": budgets,
            "budget_log": log.as_dict(),
        },
        "sources": statuses,
        "dedup_dropped": dedup_dropped,
        "records": records,
    }
