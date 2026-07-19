"""Offline selftest for pattern-scout (WSF-E00 DoD): fixture provider passes
contract tests; one dead provider doesn't kill the run; offline run serves
cached candidates stale=true; import gate hard rules; adapter isolation."""
from __future__ import annotations

import json
import os
import tempfile

from singularity.scout.fetch import BudgetLog, Fetcher, FetchResult

from . import cache as cache_mod
from .adapters import run_search
from .candidate import CandidateError, make_candidate
from .registry import load_registry

_FIXTURE_REGISTRY = {
    "schema_version": 1,
    "budgets": {"per_fetch_timeout_s": 5, "retry_max": 1,
                "retryable_classes": ["timeout", "http-5xx"],
                "wall_budget_s": 60, "max_candidates_per_provider": 10,
                "max_providers_per_run": 8, "cli_timeout_s": 5},
    "providers": [
        {"name": "fixture-good", "class": "creative-components",
         "adapter": "fixture_good", "method": "api"},
        {"name": "fixture-dead", "class": "canonical-3d",
         "adapter": "fixture_dead", "method": "api"},
        {"name": "fixture-broken", "class": "meta-registry",
         "adapter": "fixture_broken", "method": "api"},
        {"name": "fixture-paid", "class": "creative-components",
         "adapter": "fixture_paid", "method": "api"},
        {"name": "fixture-banned", "class": "inspiration-only",
         "adapter": "fixture_good", "method": "api", "enabled": False,
         "disabled_reason": "fixture disabled row"},
    ],
}


def _fixture_good(prov, queries, capabilities, ctx):
    return [
        make_candidate(prov["name"], "licensed-one", kind="component",
                       name="Licensed particles", source_url="https://ok.example/a",
                       license_str="MIT", usage_rights="commercial",
                       framework="react", install_method="shadcn",
                       capabilities=["particles"]),
        make_candidate(prov["name"], "unlicensed-one", kind="component",
                       name="Mystery component", source_url="https://ok.example/b",
                       license_str="unknown", install_method="shadcn",
                       capabilities=["background", "bogus-cap"]),
    ], "live"


def _fixture_dead(prov, queries, capabilities, ctx):
    res: FetchResult = ctx["fetch"]("https://dead.example/api")
    return [], f"unavailable:{res.error_class}"


def _fixture_broken(prov, queries, capabilities, ctx):
    raise RuntimeError("provider schema drift (fixture)")


def _fixture_paid(prov, queries, capabilities, ctx):
    return [make_candidate(prov["name"], "pro-item", kind="template",
                           name="Pro template", source_url="https://paid.example/t",
                           license_str="commercial", price_tier="paid",
                           install_method="npm")], "live"


_ADAPTERS = {"fixture_good": _fixture_good, "fixture_dead": _fixture_dead,
             "fixture_broken": _fixture_broken, "fixture_paid": _fixture_paid}


def _dead_transport(url, headers, timeout):
    return FetchResult(ok=False, status=503, error_class="http-5xx", url=url)


def run_selftest() -> tuple[list[str], dict]:
    failures: list[str] = []
    checks = 0

    def check(cond: bool, label: str):
        nonlocal checks
        checks += 1
        if not cond:
            failures.append(label)

    try:
        load_registry()
        check(True, "builtin-registry-valid")
    except Exception as exc:  # noqa: BLE001
        check(False, f"builtin-registry-valid: {exc}")

    # import gate unit rules (§2)
    rec = make_candidate("x", "1", kind="component", name="n",
                         source_url="https://e.example/", license_str="MIT",
                         install_method="source")
    check(rec["importable"] is True, "gate-licensed-importable")
    try:
        bad = dict(rec)
        bad["license"] = "unknown"
        bad["importable"] = True
        from .candidate import validate
        validate(bad)
        check(False, "gate-cannot-be-overridden")
    except CandidateError:
        check(True, "gate-cannot-be-overridden")

    with tempfile.TemporaryDirectory(prefix="pscout-selftest-") as tmp:
        old_env = os.environ.get("PSCOUT_CACHE_DIR")
        os.environ["PSCOUT_CACHE_DIR"] = tmp
        try:
            log = BudgetLog()
            fetch = Fetcher(_FIXTURE_REGISTRY["budgets"], log,
                            transport=_dead_transport)
            result = run_search(_FIXTURE_REGISTRY, ["particles"], ["particles"],
                                fetch, log, adapters=_ADAPTERS)
            prov = result["providers"]
            check(prov["fixture-good"]["status"] == "live", "good-live")
            check(prov["fixture-dead"]["status"].startswith("unavailable:"),
                  "dead-typed-does-not-kill-run")
            check(prov["fixture-broken"]["status"] == "unavailable:adapter-error",
                  "adapter-error-isolated")
            check(prov["fixture-banned"]["status"] == "skipped:disabled",
                  "disabled-typed")
            recs = {r["candidate_id"]: r for r in result["candidates"]}
            lic = recs.get("fixture-good/licensed-one")
            unl = recs.get("fixture-good/unlicensed-one")
            paid = recs.get("fixture-paid/pro-item")
            check(lic and lic["importable"], "licensed-importable")
            check(unl and not unl["importable"]
                  and "license unknown" in unl["import_block_reason"],
                  "unknown-license-blocked")
            check(unl and "bogus-cap" not in unl["capabilities"],
                  "unknown-capability-dropped")
            check(paid and not paid["importable"]
                  and "report-only" in paid["import_block_reason"],
                  "paid-report-only")

            # offline cache round: kill the good provider, expect stale serve
            def _good_now_dead(prov_cfg, queries, capabilities, ctx):
                return [], "unavailable:timeout"

            adapters2 = dict(_ADAPTERS)
            adapters2["fixture_good"] = _good_now_dead
            log2 = BudgetLog()
            fetch2 = Fetcher(_FIXTURE_REGISTRY["budgets"], log2,
                             transport=_dead_transport)
            result2 = run_search(_FIXTURE_REGISTRY, ["particles"], ["particles"],
                                 fetch2, log2, adapters=adapters2)
            good2 = result2["providers"]["fixture-good"]
            check(good2["status"] == "unavailable:timeout+cache"
                  and good2.get("stale") is True, "offline-serves-cache-status")
            stale_recs = [r for r in result2["candidates"]
                          if r["provider"] == "fixture-good"]
            check(len(stale_recs) == 2 and all(
                r["stale"] and r["health"] == "cached" for r in stale_recs),
                "cached-candidates-marked-stale")
            original = {r["candidate_id"]: r["retrieved_at"]
                        for r in result["candidates"]
                        if r["provider"] == "fixture-good"}
            check(all(original[r["candidate_id"]] == r["retrieved_at"]
                      for r in stale_recs), "stale-keeps-original-retrieved-at")

            # manifest evidence fields (DoD): license+retrieved_at always present
            check(all(r.get("license") and r.get("retrieved_at")
                      for r in result["candidates"]), "license-and-time-always-present")
        finally:
            if old_env is None:
                os.environ.pop("PSCOUT_CACHE_DIR", None)
            else:
                os.environ["PSCOUT_CACHE_DIR"] = old_env

    # cache key stability
    k1 = cache_mod.cache_key("p", ["B  ", "a"], ["3d"])
    k2 = cache_mod.cache_key("p", ["a", "b"], ["3d"])
    check(k1 == k2, "cache-key-normalized")

    # review D: a hand-built dict with importable=true is re-gated at the
    # runner boundary and dropped (validate fails on missing fields) — the
    # healthy provider still serves.
    def _rogue_adapter(prov, queries, capabilities, ctx):
        return [{"license": "unknown", "importable": True,
                 "candidate_id": "rogue/x"}], "live"

    with tempfile.TemporaryDirectory(prefix="pscout-selftest-") as tmp2:
        os.environ["PSCOUT_CACHE_DIR"] = tmp2
        try:
            adapters3 = dict(_ADAPTERS)
            adapters3["fixture_paid"] = _rogue_adapter
            log3 = BudgetLog()
            fetch3 = Fetcher(_FIXTURE_REGISTRY["budgets"], log3,
                             transport=_dead_transport)
            result3 = run_search(_FIXTURE_REGISTRY, ["particles"], [],
                                 fetch3, log3, adapters=adapters3)
            rogue_out = [r for r in result3["candidates"]
                         if r.get("candidate_id") == "rogue/x"]
            paid_status = result3["providers"]["fixture-paid"]
            check(not rogue_out and paid_status.get("dropped_invalid") == 1,
                  "runner-regates-and-drops-rogue-records")

            # review E: cached entry with importable=true + unknown license is
            # re-gated on read
            key = cache_mod.cache_key("poisoned", ["q"], [])
            poisoned = make_candidate("poisoned", "p1", kind="component",
                                      name="P", source_url="https://p.example/",
                                      license_str="MIT", install_method="source")
            poisoned["license"] = "unknown"
            poisoned["importable"] = True
            cache_mod.store(key, {"candidates": [poisoned]})
            served = cache_mod.serve_stale(cache_mod.load(key))
            check(len(served) == 1 and served[0]["importable"] is False,
                  "cache-read-regates-import")

            # review E: old-schema cache entries are refused
            import json as _json
            raw = cache_mod._path(key).read_text(encoding="utf-8")
            stale_entry = _json.loads(raw)
            stale_entry["cache_schema"] = 0
            cache_mod._path(key).write_text(_json.dumps(stale_entry),
                                            encoding="utf-8")
            check(cache_mod.load(key) is None, "old-cache-schema-refused")

            # review A/§5: use_cache=False still WRITES (reads bypassed only)
            log4 = BudgetLog()
            fetch4 = Fetcher(_FIXTURE_REGISTRY["budgets"], log4,
                             transport=_dead_transport)
            run_search(_FIXTURE_REGISTRY, ["nocache-probe"], [],
                       fetch4, log4, use_cache=False, adapters=_ADAPTERS)
            k_good = cache_mod.cache_key("fixture-good", ["nocache-probe"], [])
            check(cache_mod.load(k_good) is not None,
                  "no-cache-still-primes-writes")
        finally:
            os.environ.pop("PSCOUT_CACHE_DIR", None)

    # review B: short load-bearing tokens must match
    from .adapters import _matches
    check(_matches("webgl 3d cube scene", ["3d"]), "short-token-3d-matches")
    check(_matches("minimal ui kit", ["ui"]), "short-token-ui-matches")
    check(not _matches("plain text page", ["3d"]), "short-token-no-false-positive")

    return failures, {"checks": checks, "failures": failures}
