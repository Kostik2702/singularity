"""AUD-11: daemon singleton + PID identity.

- daemon state is written atomically with an identity (instance_id + process
  creation time);
- stop refuses to kill a PID that has been reused by an unrelated process;
- start is serialized by a lock so two concurrent starts can't both spawn;
- doctor flags an orphan daemon state (recorded but process gone).
"""
import os

import pytest

from singularity.config import Config
from singularity.platform import proc
from singularity.search import client
from singularity.search import daemon


@pytest.fixture
def cfg(tmp_path):
    return Config(vault=tmp_path, data={})


def test_pid_create_time_self():
    ct = proc.pid_create_time(os.getpid())
    # psutil is a dependency here; if absent the guard returns None (acceptable)
    assert ct is None or ct > 0


def test_write_json_atomic_roundtrip(tmp_path):
    p = tmp_path / "s.json"
    proc.write_json_atomic(p, {"a": 1, "b": "два"})
    import json
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1, "b": "два"}
    assert not list(tmp_path.glob("*.tmp"))


def _write_state(cfg, pid, create_time):
    proc.write_json_atomic(cfg.daemon_state_file,
                           {"instance_id": "abc", "pid": pid, "port": 5000,
                            "create_time": create_time})


def test_stop_kills_when_identity_matches(cfg, monkeypatch):
    killed = []
    monkeypatch.setattr(proc, "is_running", lambda pid: True)
    monkeypatch.setattr(proc, "pid_create_time", lambda pid: 111.0)
    monkeypatch.setattr(proc, "kill", lambda pid: killed.append(pid) or True)
    monkeypatch.setattr(client.time, "sleep", lambda *_a: None)
    _write_state(cfg, pid=4242, create_time=111.0)   # matches live create_time
    assert client.stop_daemon(cfg) is True
    assert killed == [4242]
    assert not cfg.daemon_state_file.exists()


def test_stop_refuses_reused_pid(cfg, monkeypatch):
    killed = []
    monkeypatch.setattr(proc, "is_running", lambda pid: True)
    monkeypatch.setattr(proc, "pid_create_time", lambda pid: 999.0)  # different!
    monkeypatch.setattr(proc, "kill", lambda pid: killed.append(pid) or True)
    _write_state(cfg, pid=4242, create_time=111.0)   # recorded != live -> reused
    assert client.stop_daemon(cfg) is False
    assert killed == []                              # unrelated process spared
    assert not cfg.daemon_state_file.exists()        # stale state cleaned


def test_stop_no_process_clears_state(cfg, monkeypatch):
    monkeypatch.setattr(proc, "is_running", lambda pid: False)
    _write_state(cfg, pid=4242, create_time=111.0)
    assert client.stop_daemon(cfg) is False
    assert not cfg.daemon_state_file.exists()


def test_start_lock_prevents_second_spawn(cfg, monkeypatch):
    spawned = []
    monkeypatch.setattr(proc, "spawn_detached", lambda *a, **k: spawned.append(a) or 1)
    monkeypatch.setattr(client.time, "sleep", lambda *_a: None)
    # simulate another starter holding the lock
    cfg.daemon_start_lock.parent.mkdir(parents=True, exist_ok=True)
    cfg.daemon_start_lock.mkdir()
    # ping returns alive shortly (the other starter's daemon)
    pings = iter([None, None, {"status": "ok"}])
    monkeypatch.setattr(client, "ping", lambda cfg: next(pings, {"status": "ok"}))
    assert client.start_daemon(cfg) is True
    assert spawned == []                             # did NOT spawn a second daemon


# --- exiting daemon must not clobber its successor's state (issue #8) --------

def _write_all_state(cfg, instance_id, pid, port=5001):
    proc.write_json_atomic(cfg.daemon_state_file,
                           {"instance_id": instance_id, "pid": pid, "port": port})
    cfg.daemon_port_file.write_text(str(port), encoding="utf-8")
    cfg.daemon_pid_file.write_text(str(pid), encoding="utf-8")


def test_dying_daemon_does_not_delete_successor_state(cfg):
    """A busy daemon outlives its SIGTERM; by the time it exits the replacement
    already owns the state files. It must leave them alone."""
    _write_all_state(cfg, instance_id="new", pid=222)
    daemon.SearchDaemon._cleanup_state(cfg, own_pid=111)          # OLD daemon exits
    assert cfg.daemon_state_file.exists()                         # successor survives
    assert cfg.daemon_port_file.exists()
    assert cfg.daemon_pid_file.exists()


def test_dying_daemon_does_not_delete_successor_state_on_pid_reuse(cfg):
    """PID reuse must not fool the ownership check — instance_id decides."""
    _write_all_state(cfg, instance_id="new", pid=111)             # same pid, new daemon
    daemon.SearchDaemon._cleanup_state(cfg, own_pid=111, own_instance_id="old")
    assert cfg.daemon_state_file.exists()
    assert cfg.daemon_port_file.exists()


def test_dying_daemon_cleans_its_own_state(cfg):
    _write_all_state(cfg, instance_id="me", pid=111)
    daemon.SearchDaemon._cleanup_state(cfg, own_pid=111, own_instance_id="me")
    assert not cfg.daemon_state_file.exists()
    assert not cfg.daemon_port_file.exists()
    assert not cfg.daemon_pid_file.exists()


def test_dying_daemon_cleans_legacy_state_without_instance_id(cfg):
    """Pre-AUD-11 state files carry no instance_id — fall back to the pid."""
    proc.write_json_atomic(cfg.daemon_state_file, {"pid": 111, "port": 5001})
    cfg.daemon_port_file.write_text("5001", encoding="utf-8")
    daemon.SearchDaemon._cleanup_state(cfg, own_pid=111, own_instance_id="me")
    assert not cfg.daemon_state_file.exists()
    assert not cfg.daemon_port_file.exists()


def test_dying_daemon_cleans_when_state_missing_or_corrupt(cfg):
    """No readable state = nobody claims ownership: the legacy port/pid files
    are this instance's leftovers, so they still go."""
    cfg.daemon_port_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.daemon_port_file.write_text("5001", encoding="utf-8")
    cfg.daemon_pid_file.write_text("111", encoding="utf-8")
    daemon.SearchDaemon._cleanup_state(cfg, own_pid=111, own_instance_id="me")
    assert not cfg.daemon_port_file.exists()
    assert not cfg.daemon_pid_file.exists()

    cfg.daemon_state_file.write_text("{not json", encoding="utf-8")
    cfg.daemon_port_file.write_text("5001", encoding="utf-8")
    daemon.SearchDaemon._cleanup_state(cfg, own_pid=111, own_instance_id="me")
    assert not cfg.daemon_state_file.exists()
    assert not cfg.daemon_port_file.exists()


def test_status_reports_orphan(cfg, monkeypatch):
    monkeypatch.setattr(proc, "is_running", lambda pid: False)
    monkeypatch.setattr(client, "ping", lambda cfg: None)
    _write_state(cfg, pid=4242, create_time=111.0)
    st = client.status(cfg)
    assert st["orphan"] is True
    assert st["pid"] == 4242
    assert st["instance_id"] == "abc"
