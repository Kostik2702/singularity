"""AUD-18: unified mb status snapshot + log rotation."""
import json

import pytest

from singularity import hooks_state, status
from singularity.config import Config
from singularity.platform import proc, scheduler
from singularity.search import client


@pytest.fixture
def cfg(tmp_path):
    return Config(vault=tmp_path, data={})


def _stub_all(monkeypatch, home=None):
    monkeypatch.setattr(client, "status",
                        lambda cfg: {"ping": None, "pid": None, "orphan": False})
    monkeypatch.setattr(scheduler, "verify", lambda job: {"registered": False})
    if home is not None:
        monkeypatch.setattr(hooks_state.Path, "home", classmethod(lambda cls: home))


def test_snapshot_has_all_sections(cfg, monkeypatch):
    _stub_all(monkeypatch, home=cfg.vault)
    snap = status.snapshot(cfg)
    for key in ("vault", "index", "daemon", "watcher", "scheduler", "disk", "hooks"):
        assert key in snap
    assert snap["index"]["present"] is False       # empty vault, no index


# --- T02: hooks section -------------------------------------------------------

def test_snapshot_hooks_section_shape(cfg, monkeypatch):
    _stub_all(monkeypatch, home=cfg.vault)
    snap = status.snapshot(cfg)
    assert snap["hooks"]["kind"] == "capture_hooks"
    assert set(snap["hooks"]["clients"]) == {"claude", "gemini", "codex"}


def test_run_human_output_mentions_capture_hooks(cfg, monkeypatch, capsys):
    _stub_all(monkeypatch, home=cfg.vault)
    status.run(cfg, as_json=False)
    assert "capture hooks:" in capsys.readouterr().out


def test_snapshot_is_json_serializable(cfg, monkeypatch):
    _stub_all(monkeypatch, home=cfg.vault)
    json.dumps(status.snapshot(cfg))               # must not raise


def test_probe_failure_is_isolated(cfg, monkeypatch):
    _stub_all(monkeypatch, home=cfg.vault)
    def boom(c):
        raise RuntimeError("disk probe blew up")
    monkeypatch.setattr(status, "_disk", boom)
    snap = status.snapshot(cfg)
    assert "error" in snap["disk"]                 # isolated, other sections fine
    assert snap["watcher"] is not None


def test_run_json_valid(cfg, monkeypatch, capsys):
    _stub_all(monkeypatch, home=cfg.vault)
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
