"""AUD-18: unified mb status snapshot + log rotation."""
import json

import pytest

from singularity import status
from singularity.config import Config
from singularity.platform import proc, scheduler
from singularity.search import client


@pytest.fixture
def cfg(tmp_path):
    return Config(vault=tmp_path, data={})


def _stub_all(monkeypatch):
    monkeypatch.setattr(client, "status",
                        lambda cfg: {"ping": None, "pid": None, "orphan": False})
    monkeypatch.setattr(scheduler, "verify", lambda job: {"registered": False})


def test_snapshot_has_all_sections(cfg, monkeypatch):
    _stub_all(monkeypatch)
    snap = status.snapshot(cfg)
    for key in ("vault", "index", "daemon", "watcher", "scheduler", "disk"):
        assert key in snap
    assert snap["index"]["present"] is False       # empty vault, no index


def test_snapshot_is_json_serializable(cfg, monkeypatch):
    _stub_all(monkeypatch)
    json.dumps(status.snapshot(cfg))               # must not raise


def test_probe_failure_is_isolated(cfg, monkeypatch):
    _stub_all(monkeypatch)
    def boom(c):
        raise RuntimeError("disk probe blew up")
    monkeypatch.setattr(status, "_disk", boom)
    snap = status.snapshot(cfg)
    assert "error" in snap["disk"]                 # isolated, other sections fine
    assert snap["watcher"] is not None


def test_run_json_valid(cfg, monkeypatch, capsys):
    _stub_all(monkeypatch)
    status.run(cfg, as_json=True)
    json.loads(capsys.readouterr().out.strip())


def test_log_rotation_shifts_and_bounds(tmp_path):
    log = tmp_path / "d.log"
    log.write_bytes(b"x" * 10)
    proc.rotate_log(log, max_bytes=5, keep=2)       # over threshold -> rotate
    assert (tmp_path / "d.log.1").exists()
    assert not log.exists() or log.stat().st_size == 0
    # second rotation shifts .1 -> .2
    log.write_bytes(b"y" * 10)
    proc.rotate_log(log, max_bytes=5, keep=2)
    assert (tmp_path / "d.log.2").exists()
    # third rotation drops the oldest (keep=2)
    log.write_bytes(b"z" * 10)
    proc.rotate_log(log, max_bytes=5, keep=2)
    assert not (tmp_path / "d.log.3").exists()


def test_log_rotation_noop_when_small(tmp_path):
    log = tmp_path / "d.log"
    log.write_bytes(b"tiny")
    proc.rotate_log(log, max_bytes=1_000_000)
    assert log.exists() and not (tmp_path / "d.log.1").exists()
