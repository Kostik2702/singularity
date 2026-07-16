"""AUD-16: mb selftest orchestration (client search/index stubbed —
no model). Verifies stage flow, pass/fail, cleanup, and --json."""
import json

import pytest

from singularity.config import Config
from singularity import selftest


@pytest.fixture
def cfg(tmp_path):
    return Config(vault=tmp_path, data={})


def _stub(monkeypatch, *, found_seq):
    """found_seq: values returned by successive _found() calls (via search)."""
    import singularity.search.client as client_mod
    monkeypatch.setattr(client_mod, "run_index",
                        lambda cfg, action="update", files=None: {"added": 1})
    calls = {"n": 0}

    def fake_search(cfg, payload):
        i = calls["n"]; calls["n"] += 1
        hit = found_seq[i] if i < len(found_seq) else False
        return {"results": [{"file": f"_selftest/{payload['query']}.md"}] if hit else []}
    monkeypatch.setattr(client_mod, "search", fake_search)


def test_happy_path_passes_and_cleans_up(cfg, monkeypatch, capsys):
    _stub(monkeypatch, found_seq=[True, False])   # found after index, gone after delete
    rc = selftest.run(cfg, as_json=False)
    assert rc == 0
    assert "OK" in capsys.readouterr().out
    # no artifacts left behind
    assert not (cfg.vault / selftest.SELFTEST_DIR).exists()


def test_find_failure_reports_and_fails(cfg, monkeypatch):
    _stub(monkeypatch, found_seq=[False, False])  # never found -> find stage fails
    rc = selftest.run(cfg, as_json=True)
    assert rc == 1


def test_verify_gone_failure(cfg, monkeypatch):
    _stub(monkeypatch, found_seq=[True, True])    # still present after delete -> fail
    rc = selftest.run(cfg, as_json=True)
    assert rc == 1


def test_json_output_is_valid(cfg, monkeypatch, capsys):
    _stub(monkeypatch, found_seq=[True, False])
    selftest.run(cfg, as_json=True)
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["ok"] is True
    assert [s["stage"] for s in payload["stages"]] == \
        ["create", "index", "find", "delete", "verify-gone"]
    assert all("ms" in s for s in payload["stages"])


def test_cleanup_on_stage_exception(cfg, monkeypatch):
    import singularity.search.client as client_mod
    monkeypatch.setattr(client_mod, "run_index",
                        lambda cfg, action="update", files=None: {"added": 1})

    def boom(cfg, payload):
        raise KeyboardInterrupt("user aborted mid-search")
    monkeypatch.setattr(client_mod, "search", boom)
    with pytest.raises(KeyboardInterrupt):
        selftest.run(cfg, as_json=True)
    # finally-block cleanup ran despite the interrupt
    assert not (cfg.vault / selftest.SELFTEST_DIR).exists()
