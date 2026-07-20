"""pattern-scout provider layer (WSF-E00) — offline contract tests.
Behavioral matrix lives in singularity.providers.selftest; here we pin it
green plus CLI exit semantics and unit rules."""
from __future__ import annotations

import json

import pytest

from singularity.providers import selftest as ps_selftest
from singularity.providers.candidate import (CandidateError, import_gate,
                                             make_candidate)
from singularity.providers.cli import main as ps_main
from singularity.providers.registry import RegistryError, load_registry


def test_selftest_green():
    failures, data = ps_selftest.run_selftest()
    assert failures == [], f"selftest failures: {failures} ({data})"
    assert data["checks"] >= 16


def test_builtin_registry_valid_and_policies():
    reg = load_registry()
    names = {p["name"] for p in reg["providers"]}
    assert {"twentyfirst", "react-bits", "three-examples",
            "motion-primitives", "gsap-demos"} <= names
    for prov in reg["providers"]:
        if prov.get("enabled") is False:
            assert prov.get("disabled_reason"), prov["name"]


def test_import_gate_rules():
    ok, _ = import_gate({"license": "MIT", "price_tier": "free",
                         "install_method": "source", "risks": []})
    assert ok
    for bad in ({"license": "unknown", "price_tier": "free",
                 "install_method": "source", "risks": []},
                {"license": "MIT", "price_tier": "paid",
                 "install_method": "npm", "risks": []},
                {"license": "MIT", "price_tier": "free",
                 "install_method": "reference-only", "risks": []},
                {"license": "MIT", "price_tier": "free",
                 "install_method": "source", "risks": ["remote-code"]}):
        allowed, reason = import_gate(bad)
        assert not allowed and reason


def test_candidate_validation_rejects_garbage():
    with pytest.raises(CandidateError):
        make_candidate("p", "x", kind="nonsense", name="n",
                       source_url="https://e.example/")
    with pytest.raises(CandidateError):
        make_candidate("p", "x", kind="component", name="n",
                       source_url="javascript:alert(1)")


def test_cli_unknown_capability_is_tool_error(capsys):
    rc = ps_main(["search", "--query", "x", "--capability", "flying-cars",
                  "--json"])
    assert rc == 2
    env = json.loads(capsys.readouterr().out)
    assert env["status"] == "error" and "flying-cars" in env["error"]


def test_cli_search_requires_query(capsys):
    rc = ps_main(["search", "--json"])
    assert rc == 2
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_cli_providers_lists(capsys):
    rc = ps_main(["providers", "--json"])
    assert rc == 0
    env = json.loads(capsys.readouterr().out)
    rows = {r["name"]: r for r in env["data"]["providers"]}
    assert rows["react-bits"]["license"] == "MIT + Commons Clause"


def test_scoring_components_and_defaults():
    from singularity.providers.scoring import hard_fail_reasons, score_candidate
    lic = make_candidate("p", "a", kind="component", name="A",
                         source_url="https://e.example/", license_str="MIT",
                         install_method="shadcn", framework="react")
    unl = make_candidate("p", "b", kind="component", name="B",
                         source_url="https://e.example/b",
                         license_str="unknown", install_method="shadcn")
    s_lic = score_candidate(lic, brief_fit=1.0, project_profile="react-motion")
    s_unl = score_candidate(unl, brief_fit=1.0, project_profile="react-motion")
    assert s_lic["components"]["license"]["points"] == 20
    assert s_unl["components"]["license"]["points"] == 0
    assert s_lic["components"]["stack"]["points"] == 15
    assert s_lic["total"] > s_unl["total"]
    # unknowns never score max
    assert s_lic["components"]["a11y"]["points"] < 10
    assert s_lic["components"]["freshness"]["points"] < 5
    # hard fails
    assert hard_fail_reasons(lic) == []
    assert any("license unknown" in r for r in hard_fail_reasons(unl))
    stale = dict(lic)
    stale["stale"] = True
    assert any("stale" in r for r in hard_fail_reasons(stale))


def test_board_renders_escaped_no_scripts():
    from singularity.providers.board import render
    from singularity.providers.scoring import score_candidate
    cand = make_candidate(
        "p", "x", kind="component", name='<script>alert(1)</script>',
        source_url="https://e.example/x", license_str="MIT",
        install_method="shadcn", description="desc <b>bold</b>")
    cand["_hard_fails"] = []
    html_text = render({"queries": ["q"], "capabilities": [],
                        "providers": {"p": {"status": "live", "results": 1}}},
                       [(cand, score_candidate(cand, brief_fit=0.5))])
    assert "<script>alert" not in html_text
    assert "&lt;script&gt;" in html_text
    assert "score evidence" in html_text


def test_bad_registry_is_tool_error(tmp_path, capsys):
    bad = tmp_path / "r.json"
    bad.write_text(json.dumps({"schema_version": 1, "budgets": {},
                               "providers": [{"name": "x", "class": "meta-registry",
                                              "method": "api"}]}),
                   encoding="utf-8")
    rc = ps_main(["providers", "--registry", str(bad), "--json"])
    assert rc == 2
    assert "adapter is required" in json.loads(capsys.readouterr().out)["error"]
