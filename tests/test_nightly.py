"""T12: `mb nightly` orchestration -- index update -> digest/curator/config
snapshot -> trend line -> markers sweep. Every snapshot is tmp+replace
atomic; one failing step never sinks the rest; the index step only ever
touches the LAST COMMITTED generation via indexer.update() (already
lock-serialized, same lock the watcher goes through) -- see
[[SINGULARITY_INCOMING_FORK_IMPLEMENTATION]] T12.
"""
from __future__ import annotations

import json
import os
import time

import numpy as np
import pytest

from singularity import nightly
from singularity.config import Config
from singularity.search import client, indexer


@pytest.fixture
def cfg(tmp_path):
    return Config(vault=tmp_path, data={})


def _chunk(file, section, type_=None, project=None):
    c = {"file": file, "section": section, "title": section}
    if type_ is not None:
        c["type"] = type_
    if project is not None:
        c["project"] = project
    return c


def _commit(cfg, pairs):
    chunks = [c for c, _ in pairs]
    embs = np.array([v for _, v in pairs], dtype=np.float32)
    indexer._commit_generation(
        cfg.memory_index_dir, embs,
        {"model": "m", "file_meta": {}, "chunks": chunks})


def _no_index_update(monkeypatch):
    # `corpus` accepted since W03: the sources step routes through the same
    # client call, and a stub with the old signature would TypeError there.
    monkeypatch.setattr(client, "run_index",
                        lambda cfg, action, files=None, corpus=None: {"added": 0})


# --- index step: lock handling ------------------------------------------------

def test_locked_index_step_is_skipped_not_raised(cfg, monkeypatch):
    def boom(cfg, action, files=None):
        raise client.RemoteIndexError("locked", error_type="IndexLockedError")
    monkeypatch.setattr(client, "run_index", boom)
    result = nightly.run(cfg)
    assert result["steps"]["index"] == {"status": "skipped", "reason": "locked"}


def test_index_step_delegates_to_daemon(cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(client, "run_index",
                        lambda cfg, action, files=None: calls.append(action) or {"added": 0})
    nightly.run(cfg)
    assert calls == ["update"]


# --- digest snapshot -----------------------------------------------------------

def test_digest_snapshot_written_atomically(cfg, monkeypatch):
    _no_index_update(monkeypatch)
    _commit(cfg, [
        (_chunk("learnings/active/T.md", "S1", "learning"), [1, 0]),
        (_chunk("reference/R.md", "R1", "reference"), [1, 0]),
    ])
    nightly.run(cfg)
    data = json.loads((cfg.mb_dir / nightly.DIGEST_SNAPSHOT_NAME).read_text(encoding="utf-8"))
    assert data["index_status"] == "ok"
    assert data["entries"][0]["file"] == "learnings/active/T.md"
    leftovers = [p for p in cfg.mb_dir.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


# --- curator snapshot ------------------------------------------------------------

def test_curator_snapshot_written(cfg, monkeypatch):
    _no_index_update(monkeypatch)
    nightly.run(cfg)
    data = json.loads((cfg.mb_dir / nightly.CURATOR_SNAPSHOT_NAME).read_text(encoding="utf-8"))
    assert "issues" in data and "notes" in data


def test_curator_snapshot_includes_t13_lint(cfg, monkeypatch):
    _no_index_update(monkeypatch)
    result = nightly.run(cfg)
    data = json.loads((cfg.mb_dir / nightly.CURATOR_SNAPSHOT_NAME).read_text(encoding="utf-8"))
    assert "lint" in data
    assert "duplicates" in data["lint"] and "supersession" in data["lint"]
    assert "duplicates" in result["steps"]["curator"]
    assert "supersession" in result["steps"]["curator"]


# --- config snapshot -------------------------------------------------------------

def test_config_snapshot_reports_validation_errors(cfg, monkeypatch):
    _no_index_update(monkeypatch)
    cfg.data["min_score"] = 5.0   # out of range -> a validation error
    nightly.run(cfg)
    data = json.loads((cfg.mb_dir / nightly.CONFIG_SNAPSHOT_NAME).read_text(encoding="utf-8"))
    assert data["errors"]


def test_config_snapshot_clean_on_default_config(cfg, monkeypatch):
    _no_index_update(monkeypatch)
    nightly.run(cfg)
    data = json.loads((cfg.mb_dir / nightly.CONFIG_SNAPSHOT_NAME).read_text(encoding="utf-8"))
    assert data["errors"] == []


# --- markers sweep -----------------------------------------------------------------

def test_markers_sweep_removes_only_stale_files(cfg, monkeypatch):
    _no_index_update(monkeypatch)
    markers = cfg.markers_dir
    markers.mkdir(parents=True)
    old = markers / "capture-old"
    old.write_text("x", encoding="utf-8")
    fresh = markers / "capture-fresh"
    fresh.write_text("x", encoding="utf-8")
    old_time = time.time() - 40 * 86400
    os.utime(old, (old_time, old_time))
    cfg.data["markers_retention_days"] = 30
    result = nightly.run(cfg)
    assert not old.exists()
    assert fresh.exists()
    assert result["steps"]["markers"]["swept"] == 1


def test_markers_sweep_noop_when_dir_absent(cfg, monkeypatch):
    _no_index_update(monkeypatch)
    result = nightly.run(cfg)
    assert result["steps"]["markers"] == {"status": "ok", "swept": 0}


# --- trend line -----------------------------------------------------------------

def test_trend_line_appends_one_row_per_run(cfg, monkeypatch):
    _no_index_update(monkeypatch)
    nightly.run(cfg)
    nightly.run(cfg)
    rows = [json.loads(l) for l in
           (cfg.mb_dir / nightly.TREND_HISTORY_NAME).read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2


def test_trend_line_prunes_rows_older_than_retention(cfg, monkeypatch):
    _no_index_update(monkeypatch)
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    (cfg.mb_dir / nightly.TREND_HISTORY_NAME).write_text(
        json.dumps({"ts": "2020-01-01T00:00:00", "index_status": None}) + "\n", encoding="utf-8")
    cfg.data["nightly_trend_retention_days"] = 1
    nightly.run(cfg)
    rows = [json.loads(l) for l in
           (cfg.mb_dir / nightly.TREND_HISTORY_NAME).read_text(encoding="utf-8").splitlines()]
    assert all(r["ts"] != "2020-01-01T00:00:00" for r in rows)


def test_trend_line_tolerates_malformed_rows(cfg, monkeypatch):
    _no_index_update(monkeypatch)
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    (cfg.mb_dir / nightly.TREND_HISTORY_NAME).write_text("{not json\n", encoding="utf-8")
    result = nightly.run(cfg)   # must not raise
    assert result["steps"]["trend"]["status"] == "ok"


def test_trend_line_no_leftover_tmp(cfg, monkeypatch):
    _no_index_update(monkeypatch)
    nightly.run(cfg)
    leftovers = [p for p in cfg.mb_dir.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


# --- step isolation ----------------------------------------------------------------

def test_one_failing_step_does_not_sink_the_rest(cfg, monkeypatch):
    _no_index_update(monkeypatch)
    import singularity.curator as curator_mod

    def boom(cfg):
        raise RuntimeError("curator blew up")
    monkeypatch.setattr(curator_mod, "validate", boom)
    result = nightly.run(cfg)
    assert result["steps"]["curator"]["status"] == "error"
    assert result["steps"]["digest"]["status"] == "ok"
    assert result["steps"]["config"]["status"] == "ok"
    assert result["steps"]["trend"]["status"] == "ok"


def test_has_error_true_when_any_step_errors(cfg, monkeypatch):
    _no_index_update(monkeypatch)
    import singularity.curator as curator_mod
    monkeypatch.setattr(curator_mod, "validate",
                        lambda cfg: (_ for _ in ()).throw(RuntimeError("x")))
    result = nightly.run(cfg)
    assert nightly.has_error(result)


def test_has_error_false_on_clean_run(cfg, monkeypatch):
    _no_index_update(monkeypatch)
    result = nightly.run(cfg)
    assert not nightly.has_error(result)


# --- scheduler migration -----------------------------------------------------------

def test_migrate_scheduler_job_unregisters_legacy_and_registers_new(monkeypatch, cfg):
    from singularity.platform import scheduler
    calls = []
    monkeypatch.setattr(scheduler, "verify",
                        lambda job: {"registered": job.name == "nightly-index"})
    monkeypatch.setattr(scheduler, "unregister",
                        lambda job: calls.append(("unregister", job.name)))
    monkeypatch.setattr(scheduler, "register",
                        lambda job: calls.append(("register", job.name)) or "OK")
    res = nightly.migrate_scheduler_job(cfg)
    assert res == "OK"
    assert ("unregister", "nightly-index") in calls
    assert ("register", "nightly") in calls


def test_migrate_scheduler_job_skips_unregister_when_no_legacy(monkeypatch, cfg):
    from singularity.platform import scheduler
    calls = []
    monkeypatch.setattr(scheduler, "verify", lambda job: {"registered": False})
    monkeypatch.setattr(scheduler, "unregister",
                        lambda job: calls.append(("unregister", job.name)))
    monkeypatch.setattr(scheduler, "register", lambda job: "OK")
    nightly.migrate_scheduler_job(cfg)
    assert calls == []
