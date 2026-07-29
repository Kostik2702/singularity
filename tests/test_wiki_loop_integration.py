"""W11: the ingest cycle has to remind you it exists.

nightly steps (each independently guarded), trend fields, the SessionStart
nag, sources sections in status/stats, and the strictest block in
`mb doctor` — the one guarding the only data a rebuild cannot recreate.
"""
from __future__ import annotations

import json

import pytest

from singularity import doctor, nightly, sources, stats, status
from singularity.config import Config
from singularity.hooks import session_start


@pytest.fixture(autouse=True)
def no_daemon(monkeypatch):
    """`nightly.run` drives indexing through the warm daemon. Unstubbed, every
    step here would try to SPAWN one against a temp vault and block on the 30s
    start timeout — measured at 333s for this file before the stub."""
    from singularity.search import client

    monkeypatch.setattr(client, "run_index",
                        lambda cfg, action, files=None, corpus=None: {"added": 0})
    monkeypatch.setattr(client, "ping", lambda cfg: None)
    monkeypatch.setattr(client, "start_daemon", lambda cfg, wait=True: False)


@pytest.fixture
def cfg(tmp_path):
    c = Config(vault=tmp_path, data={})
    c.mb_dir.mkdir(parents=True, exist_ok=True)
    return c


def _report(cfg, fix=False):
    rep = doctor.Report(quiet=True)
    doctor.wiki_loop_block(rep, cfg, fix=fix)
    return {c["id"]: c for c in rep.checks}


# --- doctor: block W --------------------------------------------------------

def test_clean_slate_passes(cfg):
    checks = _report(cfg)
    assert checks["W1-sources-integrity"]["status"] == "PASS"
    assert checks["W2-sources-index"]["status"] == "PASS"


def test_registry_without_directory_is_a_hard_fail(cfg):
    """The habit this guards against: 'delete .mb and re-index'."""
    import shutil
    sources.add(cfg, "# Doc\n\nbody", origin_ref="ref")
    shutil.rmtree(cfg.sources_dir)
    check = _report(cfg)["W1-sources-integrity"]
    assert check["status"] == "FAIL"
    assert "backup restore" in check["fix"]


def test_registry_entry_without_a_file_is_a_hard_fail(cfg):
    added = sources.add(cfg, "# Doc\n\nbody", origin_ref="ref")
    sources.stored_abspath(cfg, added["stored_path"]).unlink()
    assert _report(cfg)["W1-sources-integrity"]["status"] == "FAIL"


def test_orphan_raw_file_is_only_a_warning(cfg):
    sources.add(cfg, "# Doc\n\nbody", origin_ref="ref")
    orphan = cfg.sources_dir / "2026" / "stray--deadbeef.md"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("---\ntype: source\n---\n\nstray", encoding="utf-8")
    check = _report(cfg)["W1-sources-integrity"]
    assert check["status"] == "WARN" and "no registry entry" in check["detail"]


def test_raw_without_a_registry_is_a_warning_not_a_fail(cfg):
    sources.add(cfg, "# Doc\n\nbody", origin_ref="ref")
    sources.registry_path(cfg).unlink()
    assert _report(cfg)["W1-sources-integrity"]["status"] == "WARN"


def test_doctor_fix_never_deletes_raw(cfg):
    """Acceptance: --fix is strictly additive on this block."""
    import hashlib
    added = sources.add(cfg, "# Doc\n\nbody", origin_ref="ref")
    path = sources.stored_abspath(cfg, added["stored_path"])
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    _report(cfg, fix=True)
    assert path.is_file()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_missing_corpus_is_a_warning_with_the_command(cfg):
    sources.add(cfg, "# Doc\n\nbody", origin_ref="ref")
    check = _report(cfg)["W2-sources-index"]
    assert check["status"] == "WARN" and "mb ingest index" in check["fix"]


def test_kill_switch_skips_the_index_check(cfg):
    cfg.data["ingest_index_sources"] = False
    sources.add(cfg, "# Doc\n\nbody", origin_ref="ref")
    assert _report(cfg)["W2-sources-index"]["status"] == "SKIP"


def test_model_mismatch_is_reported(cfg, monkeypatch):
    from singularity.search import indexer
    sources.add(cfg, "# Doc\n\nbody", origin_ref="ref")
    summaries = {str(cfg.sources_index_dir): {"model": "a", "indexed_at": "2030-01-01",
                                              "chunk_count": 1},
                 str(cfg.memory_index_dir): {"model": "b"}}
    monkeypatch.setattr(indexer, "index_summary",
                        lambda d: summaries.get(str(d)))
    check = _report(cfg)["W2-sources-index"]
    assert check["status"] == "WARN" and "different models" in check["detail"]


def test_stale_corpus_is_reported(cfg, monkeypatch):
    from singularity.search import indexer
    sources.add(cfg, "# Doc\n\nbody", origin_ref="ref", now="2030-01-01T00:00:00")
    monkeypatch.setattr(indexer, "index_summary", lambda d: {
        "model": None, "indexed_at": "2020-01-01T00:00:00", "chunk_count": 1})
    check = _report(cfg)["W2-sources-index"]
    assert check["status"] == "WARN" and "older than the registry" in check["detail"]


def test_git_check_is_skipped_outside_a_repo(cfg):
    assert _report(cfg)["W3-sources-portability"]["status"] == "SKIP"


def test_gitignored_sources_are_warned_about(cfg, monkeypatch):
    import subprocess
    (cfg.vault / ".git").mkdir()
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "git")
    monkeypatch.setattr(
        doctor.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, b"", b""))
    check = _report(cfg)["W3-sources-portability"]
    assert check["status"] == "WARN" and "git-ignored" in check["detail"]


# --- nightly ----------------------------------------------------------------

def test_new_steps_are_present(cfg):
    result = nightly.run(cfg)
    for step in ("ingest_index", "gaps", "contradictions", "sources", "ingest_log"):
        assert step in result["steps"], step


def test_a_failing_gaps_step_does_not_sink_the_chain(cfg, monkeypatch):
    """Acceptance: injected exception in one step, the rest still run."""
    def boom(*_a, **_k):
        raise RuntimeError("injected")

    monkeypatch.setattr(nightly, "_step_gaps", boom)
    result = nightly.run(cfg)
    assert result["steps"]["gaps"]["status"] == "error"
    assert "injected" in result["steps"]["gaps"]["error"]
    assert result["steps"]["config"]["status"] == "ok"
    assert result["steps"]["trend"]["status"] == "ok"
    assert nightly.has_error(result) is True


def test_gaps_snapshot_is_written(cfg):
    nightly.run(cfg)
    snapshot = json.loads((cfg.mb_dir / nightly.GAPS_SNAPSHOT_NAME)
                          .read_text(encoding="utf-8"))
    assert "counts" in snapshot and "generated_at" in snapshot


def test_contradictions_snapshot_is_written(cfg):
    nightly.run(cfg)
    snapshot = json.loads((cfg.mb_dir / nightly.CONTRADICTIONS_SNAPSHOT_NAME)
                          .read_text(encoding="utf-8"))
    assert "band" in snapshot


def test_trend_row_carries_the_ingest_fields(cfg):
    sources.add(cfg, "# Doc\n\nbody", origin_ref="ref")
    nightly.run(cfg)
    rows = [json.loads(line) for line in
            (cfg.mb_dir / nightly.TREND_HISTORY_NAME).read_text(encoding="utf-8").splitlines()]
    row = rows[-1]
    for key in ("sources", "unprocessed_sources", "open_questions",
                "contradiction_candidates"):
        assert key in row, key
    assert row["sources"] == 1 and row["unprocessed_sources"] == 1


def test_ingest_index_step_skips_when_disabled(cfg):
    cfg.data["ingest_index_sources"] = False
    sources.add(cfg, "# Doc\n\nbody", origin_ref="ref")
    step = nightly.run(cfg)["steps"]["ingest_index"]
    assert step["status"] == "skipped" and "ingest_index_sources" in step["reason"]


def test_ingest_index_step_skips_without_sources(cfg):
    assert nightly.run(cfg)["steps"]["ingest_index"]["reason"] == "no sources"


def test_log_retention_runs_in_nightly(cfg):
    sid = sources.add(cfg, "# Doc\n\nbody", origin_ref="ref")["source_id"]
    sources.attach_notes(cfg, sid, ["old.md"], log=True, now="2000-01-01T00:00:00")
    sources.attach_notes(cfg, sid, ["new.md"], log=True)
    assert nightly.run(cfg)["steps"]["ingest_log"] == {"status": "ok", "kept": 1,
                                                       "swept": 1}


# --- status / stats ---------------------------------------------------------

def test_status_reports_sources(cfg):
    sources.add(cfg, "# Doc\n\nbody", origin_ref="ref")
    snap = status.snapshot(cfg)["sources"]
    assert snap["present"] is True and snap["documents"] == 1
    assert snap["unprocessed"] == 1 and snap["corpus_enabled"] is True


def test_status_is_quiet_without_sources(cfg):
    assert status.snapshot(cfg)["sources"] == {"present": False}


def test_stats_reports_sources(cfg):
    sid = sources.add(cfg, "# Doc\n\nbody", origin_ref="ref")["source_id"]
    sources.attach_notes(cfg, sid, ["reference/a.md"], status="processed")
    out = stats.compute(cfg)["sources"]
    assert out["documents"] == 1 and out["stored"] == 1
    assert out["by_status"] == {"processed": 1} and out["derived_notes"] == 1


# --- SessionStart nag -------------------------------------------------------

def _context(vault, monkeypatch):
    monkeypatch.setattr("singularity.config.resolve_vault", lambda _x=None: vault)
    return session_start.build_context(str(vault))


def test_nag_fires_for_an_unprocessed_source(cfg, monkeypatch):
    sources.add(cfg, "# Doc\n\nbody", origin_ref="ref")
    text = _context(cfg.vault, monkeypatch)
    assert "ingest backlog" in text and "mb ingest plan" in text


def test_nag_is_rate_limited(cfg, monkeypatch):
    sources.add(cfg, "# Doc\n\nbody", origin_ref="ref")
    assert "ingest backlog" in _context(cfg.vault, monkeypatch)
    assert "ingest backlog" not in _context(cfg.vault, monkeypatch)


def test_nag_is_silent_when_everything_is_processed(cfg, monkeypatch):
    sid = sources.add(cfg, "# Doc\n\nbody", origin_ref="ref")["source_id"]
    sources.attach_notes(cfg, sid, ["reference/a.md"], status="processed")
    assert "ingest backlog" not in _context(cfg.vault, monkeypatch)


def test_the_two_nags_do_not_reset_each_other(cfg, monkeypatch):
    """Both stamps live in nag-state.json; a merge bug would make one nag
    silence the other forever."""
    state = cfg.mb_dir / "nag-state.json"
    session_start._stamp_nag(state, "last_shown_at")
    session_start._stamp_nag(state, "ingest_last_shown_at")
    stored = json.loads(state.read_text(encoding="utf-8"))
    assert set(stored) == {"last_shown_at", "ingest_last_shown_at"}


def test_a_broken_registry_never_breaks_the_session(cfg, monkeypatch):
    sources.registry_path(cfg).write_text("{broken", encoding="utf-8")
    text = _context(cfg.vault, monkeypatch)
    assert "[Singularity memory]" in text and "ingest backlog" not in text


def test_nag_respects_the_line_budget(cfg, monkeypatch):
    sources.add(cfg, "# Doc\n\nbody", origin_ref="ref")
    (cfg.mb_dir / "config.json").write_text(
        json.dumps({"session_context_max_lines": 3}), encoding="utf-8")
    text = _context(cfg.vault, monkeypatch)
    assert len(text.splitlines()) <= 4      # 3 + the truncation notice
