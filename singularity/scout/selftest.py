"""Offline selftest for design-scout: canned fetcher, no network. Exercises
the collect contract end-to-end plus the negative guarantees (§2-§8). The
pytest suite reuses these checks; the CLI exposes them as `selftest` so a
clean agent session can verify the tool without writing any script."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from ._contract import ContractError, ENGINE_ROOT, resolve_out
from .collect import run_collect
from .fetch import BudgetLog, Fetcher, FetchResult
from .moodboard import render
from .registry import load_registry

_OPENVERSE_PAYLOAD = {
    "results": [
        {"title": "Green terminal", "license": "by", "license_version": "2.0",
         "thumbnail": "https://fixture.local/thumb1.jpg",
         "foreign_landing_url": "https://example.org/work/1", "creator": "Ada"},
        {"title": "No-license item (must drop)", "license": None,
         "thumbnail": "https://fixture.local/thumb2.jpg",
         "foreign_landing_url": "https://example.org/work/2", "creator": "Bob"},
        {"title": "Dup of gallery card", "license": "cc0",
         "thumbnail": "https://fixture.local/thumb3.jpg",
         "foreign_landing_url": "https://showcase.example/site/a", "creator": "Cy"},
        {"title": "javascript scheme (must drop)", "license": "cc0",
         "thumbnail": "https://fixture.local/thumb4.jpg",
         "foreign_landing_url": "javascript:alert(document.title)", "creator": "Ev"},
        {"title": "empty landing (must drop)", "license": "cc0",
         "thumbnail": "https://fixture.local/thumb5.jpg",
         "foreign_landing_url": "", "creator": "Nul"},
    ]
}

_BROKEN_PAYLOAD = {"results": ["schema-drift-string", 42]}

_GALLERY_HTML = """<html><body>
<a href="https://showcase.example/site/a"><img src="/t/a.png" alt="Site A"></a>
<a href="https://showcase.example/site/b"><img src="/t/b.png" alt="Site B"></a>
<a href="/nav/about">About</a>
<article><figure><img src="/t/c.png" alt=""></figure>
<div><a href="/website/site-c/">Site C Showcase</a>
<a href="https://site-c.example/?ref=gallery">Visit</a></div></article>
<article><p>no image card — must be ignored</p><a href="/website/empty/">Empty</a></article>
</body></html>"""


def _fixture_registry() -> dict:
    return {
        "schema_version": 1,
        "budgets": {"per_fetch_timeout_s": 5, "max_results_per_source": 10,
                    "max_thumb_downloads": 5, "max_sources_per_run": 8,
                    "wall_budget_s": 60, "retry_max": 1,
                    "retryable_classes": ["timeout", "http-5xx"],
                    "floor_live_total": 2, "floor_live_api": 1},
        "sources": [
            {"name": "openverse", "method": "api", "enabled": True,
             "license_policy": "api-free-license",
             "probe_url": "https://fixture.local/api",
             "search_endpoint": "https://fixture.local/api?q={query}&per={per}"},
            {"name": "gallery", "method": "public-html", "enabled": True,
             "license_policy": "links-only",
             "probe_url": "https://showcase.example/",
             "search_endpoint": "https://showcase.example/?s={query}"},
            {"name": "flaky", "method": "public-html", "enabled": True,
             "license_policy": "links-only",
             "probe_url": "https://flaky.example/",
             "search_endpoint": "https://flaky.example/?s={query}"},
            {"name": "dead", "method": "public-html", "enabled": True,
             "license_policy": "links-only",
             "probe_url": "https://dead.example/",
             "search_endpoint": "https://dead.example/?s={query}"},
            {"name": "banned", "method": "public-html", "enabled": False,
             "license_policy": "links-only",
             "probe_url": "https://banned.example/",
             "disabled_reason": "robots disallows AI crawlers (fixture)"},
            {"name": "keyed", "method": "api", "enabled": True,
             "license_policy": "api-hotlink", "key_env": "DSCOUT_SELFTEST_NO_SUCH_KEY",
             "probe_url": "https://keyed.example/",
             "search_endpoint": "https://keyed.example/?q={query}&per={per}"},
        ],
    }


class _CannedTransport:
    """Deterministic transport double behind the REAL Fetcher (so retry and
    budget code paths are the ones under test): 'flaky' fails once with 5xx
    then succeeds (proves retry-by-class), 'dead' always 403 (proves no-retry
    + typed status), thumbnails return bytes."""

    def __init__(self):
        self.calls: list[str] = []
        self._flaky_failed = False

    def __call__(self, url: str, headers: dict, timeout: float) -> FetchResult:
        self.calls.append(url)
        if url.startswith("https://broken.local/api"):
            return FetchResult(ok=True, status=200, url=url,
                               body=json.dumps(_BROKEN_PAYLOAD).encode())
        if url.startswith("https://fixture.local/api"):
            return FetchResult(ok=True, status=200, url=url,
                               body=json.dumps(_OPENVERSE_PAYLOAD).encode())
        if url.startswith("https://fixture.local/thumb"):
            return FetchResult(ok=True, status=200, url=url, body=b"PNGBYTES")
        if url.startswith("https://showcase.example"):
            return FetchResult(ok=True, status=200, url=url,
                               body=_GALLERY_HTML.encode())
        if url.startswith("https://flaky.example"):
            if not self._flaky_failed:
                self._flaky_failed = True
                return FetchResult(ok=False, status=503, error_class="http-5xx",
                                   url=url)
            return FetchResult(ok=True, status=200, url=url,
                               body=_GALLERY_HTML.replace(
                                   "showcase.example", "flaky-sites.example").encode())
        if url.startswith("https://dead.example"):
            return FetchResult(ok=False, status=403, error_class="http-403",
                               url=url)
        return FetchResult(ok=False, error_class="dns", url=url)


def run_selftest() -> tuple[list[str], dict]:
    failures: list[str] = []
    checks = 0

    def check(cond: bool, label: str):
        nonlocal checks
        checks += 1
        if not cond:
            failures.append(label)

    # builtin registry must validate
    try:
        load_registry()
        check(True, "registry-valid")
    except ContractError as exc:
        check(False, f"registry-valid: {exc}")

    # §3: engine repo is always a protected root
    try:
        resolve_out(str(ENGINE_ROOT / "scratch"))
        check(False, "resolve-out-protects-engine")
    except ContractError:
        check(True, "resolve-out-protects-engine")

    registry = _fixture_registry()
    log = BudgetLog()
    transport = _CannedTransport()
    fetch = Fetcher(registry["budgets"], log, transport=transport)
    with tempfile.TemporaryDirectory(prefix="dscout-selftest-") as tmp:
        out_dir = Path(tmp)
        manifest = run_collect(registry, ["dark terminal"], out_dir, fetch, log)
        sources = manifest["sources"]
        check(sources["openverse"]["status"] == "live", "openverse-live")
        check(sources["dead"]["status"] == "unavailable:http-403",
              "dead-typed-unavailable")
        check(sources["flaky"]["status"] == "live" and log.retries >= 1,
              "flaky-retried-by-class")
        check(sources["banned"]["status"] == "skipped:disabled",
              "disabled-typed-skip")
        check(sources["keyed"]["status"] == "skipped:no-key", "no-key-typed-skip")

        titles = [r["title"] for r in manifest["records"]]
        check("No-license item (must drop)" not in titles,
              "unlicensed-api-item-dropped")
        check(manifest["dedup_dropped"] >= 1, "dedup-dropped-counted")

        thumbs = list((out_dir / "thumbs").glob("*")) if (out_dir / "thumbs").exists() else []
        api_free = [r for r in manifest["records"]
                    if r["license_policy"] == "api-free-license" and r["local_thumb"]]
        links_only_with_file = [r for r in manifest["records"]
                                if r["license_policy"] != "api-free-license"
                                and r["local_thumb"]]
        check(len(api_free) >= 1 and len(thumbs) == len(api_free),
              "thumbs-only-for-api-free")
        check(not links_only_with_file, "links-only-never-downloads")

        # pattern-2 (article container): external anchor wins, titled anchor
        # names the card; an image-less article is ignored.
        art_cards = [r for r in manifest["records"]
                     if r["page_url"].startswith("https://site-c.example")]
        check(len(art_cards) >= 1 and art_cards[0]["title"] == "Site C Showcase",
              "article-card-external-href-titled")
        check(not any("website/empty" in r["page_url"] for r in manifest["records"]),
              "imageless-article-ignored")

        html_text = render(manifest)
        check("Site B" in html_text and "unavailable:http-403" in html_text,
              "moodboard-renders-cards-and-statuses")
        # §8 first-occurrence-wins: showcase.example/site/a exists exactly once
        # and belongs to openverse (earlier in registry order than gallery).
        dup_claimants = [r for r in manifest["records"]
                         if r["page_url"].startswith("https://showcase.example/site/a")]
        check(len(dup_claimants) == 1 and dup_claimants[0]["source"] == "openverse",
              "dedup-first-occurrence-wins")
        check("<script" not in html_text.lower(), "moodboard-no-scripts")

        # review F2/F5: hostile schemes and empty landing URLs never survive
        check(not any(r["page_url"].startswith("javascript:")
                      for r in manifest["records"]), "javascript-scheme-dropped")
        check("javascript:" not in html_text, "moodboard-no-js-hrefs")
        check(all(r["page_url"] for r in manifest["records"]),
              "empty-page-url-dropped")

        # review F8: http-403 is never retried — exactly one attempt per query
        dead_calls = [c for c in transport.calls
                      if c.startswith("https://dead.example")]
        check(len(dead_calls) == 1, "http-403-never-retried")

    # review F1: provider schema drift is isolated, the run survives
    broken_reg = _fixture_registry()
    broken_reg["sources"] = [
        {"name": "openverse", "method": "api", "enabled": True,
         "license_policy": "api-free-license",
         "probe_url": "https://broken.local/api",
         "search_endpoint": "https://broken.local/api?q={query}&per={per}"},
        broken_reg["sources"][1],  # healthy gallery
    ]
    log2 = BudgetLog()
    fetch2 = Fetcher(broken_reg["budgets"], log2, transport=_CannedTransport())
    with tempfile.TemporaryDirectory(prefix="dscout-selftest-") as tmp2:
        man2 = run_collect(broken_reg, ["dark"], Path(tmp2), fetch2, log2)
        check(man2["sources"]["openverse"]["status"] == "unavailable:adapter-error",
              "adapter-error-typed")
        check(man2["sources"]["gallery"]["status"] == "live"
              and len(man2["records"]) >= 1, "run-survives-broken-provider")

    # review F4: budget caps actually bite and report typed skips
    tight = _fixture_registry()
    tight["budgets"]["max_sources_per_run"] = 1
    tight["budgets"]["max_thumb_downloads"] = 1
    log3 = BudgetLog()
    fetch3 = Fetcher(tight["budgets"], log3, transport=_CannedTransport())
    with tempfile.TemporaryDirectory(prefix="dscout-selftest-") as tmp3:
        man3 = run_collect(tight, ["dark"], Path(tmp3), fetch3, log3)
        check(man3["sources"]["openverse"]["status"] == "live"
              and man3["sources"]["gallery"]["status"] == "skipped:budget",
              "max-sources-cap-typed-skip")
        check(log3.thumb_downloads == 1
              and log3.stop_condition == "max_thumb_downloads",
              "thumb-cap-stops-and-logs")

    walled = _fixture_registry()
    walled["budgets"]["wall_budget_s"] = 0
    log4 = BudgetLog()
    fetch4 = Fetcher(walled["budgets"], log4, transport=_CannedTransport())
    with tempfile.TemporaryDirectory(prefix="dscout-selftest-") as tmp4:
        man4 = run_collect(walled, ["dark"], Path(tmp4), fetch4, log4)
        check(man4["sources"]["openverse"]["status"] == "skipped:budget",
              "wall-budget-typed-skip")

    # review F3: probe floor logic, both branches, offline
    from .collect import run_probe
    p_results, p_passed, _ = run_probe(_fixture_registry(),
                                       transport=_CannedTransport())
    check(p_passed and p_results["openverse"]["status"] == "live"
          and p_results["dead"]["status"].startswith("unavailable:"),
          "probe-floor-pass")
    high = _fixture_registry()
    high["budgets"]["floor_live_total"] = 99
    _, p_failed, _ = run_probe(high, transport=_CannedTransport())
    check(not p_failed, "probe-floor-fail-branch")

    return failures, {"checks": checks, "failures": failures}
