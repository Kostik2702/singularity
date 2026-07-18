"""design-scout (WSF-09) — contract tests, fully offline.

The heavy behavioral matrix lives in singularity.scout.selftest (canned
transport behind the real Fetcher); pytest asserts it stays green and adds
CLI-level contract checks (exit semantics, JSON envelope on bad args,
registry validation, --out protection)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from singularity.scout import selftest as scout_selftest
from singularity.scout._contract import ContractError, resolve_out
from singularity.scout.cli import main as scout_main
from singularity.scout.registry import load_registry

ENGINE = Path(__file__).resolve().parents[1]


def test_selftest_green():
    failures, data = scout_selftest.run_selftest()
    assert failures == [], f"selftest failures: {failures} ({data})"
    assert data["checks"] >= 16


def test_builtin_registry_valid():
    reg = load_registry()
    names = [s["name"] for s in reg["sources"]]
    assert names[0] == "openverse"  # §8 registry order is meaningful
    # OD-2: gallery sources must never be downloadable policies
    for src in reg["sources"]:
        if src["method"] == "public-html":
            assert src["license_policy"] == "links-only", src["name"]
    # §7: every disabled source explains itself
    for src in reg["sources"]:
        if src.get("enabled") is False:
            assert src.get("disabled_reason"), src["name"]


def test_bad_registry_is_tool_error(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema_version": 99, "budgets": {}, "sources": []}),
                   encoding="utf-8")
    with pytest.raises(ContractError):
        load_registry(bad)


def test_registry_disabled_requires_reason(tmp_path):
    reg = {"schema_version": 1, "budgets": {},
           "sources": [{"name": "x", "method": "api",
                        "license_policy": "links-only", "enabled": False}]}
    p = tmp_path / "r.json"
    p.write_text(json.dumps(reg), encoding="utf-8")
    with pytest.raises(ContractError):
        load_registry(p)


def test_resolve_out_refuses_engine_and_protected(tmp_path, monkeypatch):
    with pytest.raises(ContractError):
        resolve_out(str(ENGINE / "sub"))
    guarded = tmp_path / "project"
    monkeypatch.setenv("DSCOUT_PROTECTED_ROOTS", str(guarded))
    with pytest.raises(ContractError):
        resolve_out(str(guarded / "evidence"))
    ok = resolve_out(str(tmp_path / "free"))
    assert ok.is_dir()


def test_collect_requires_query(tmp_path, capsys):
    rc = scout_main(["collect", "--out", str(tmp_path / "o"), "--json"])
    assert rc == 2
    env = json.loads(capsys.readouterr().out)
    assert env["status"] == "error" and env["schema_version"] == 1


def test_argparse_error_keeps_json_contract(capsys):
    rc = scout_main(["no-such-command"])
    assert rc == 2
    out = capsys.readouterr().out
    env = json.loads(out)
    assert env["status"] == "error"


def test_sources_command_exit_zero(capsys):
    rc = scout_main(["sources", "--json"])
    assert rc == 0
    env = json.loads(capsys.readouterr().out)
    rows = {r["name"]: r for r in env["data"]["sources"]}
    assert rows["godly"]["enabled"] is False
    assert rows["unsplash"]["key_env"] == "UNSPLASH_ACCESS_KEY"


def test_selftest_cli_exit_semantics():
    proc = subprocess.run(
        [sys.executable, "-m", "singularity.scout.cli", "selftest", "--json"],
        capture_output=True, text=True, encoding="utf-8", cwd=str(ENGINE),
        timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    env = json.loads(proc.stdout)
    assert env["verdict"]["result"] == "PASS"
