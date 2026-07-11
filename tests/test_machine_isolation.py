"""AUD-19: machine-local runtime isolation over a shared/synced vault."""
import pytest

from singularity import config as cfgmod
from singularity.config import Config
from singularity.platform import proc
from singularity.search import client


@pytest.fixture
def cfg(tmp_path):
    return Config(vault=tmp_path, data={})


def test_machine_id_is_stable_and_sanitized(monkeypatch):
    monkeypatch.setattr(cfgmod.platform, "node", lambda: "My PC / weird:name")
    a = cfgmod.machine_id()
    b = cfgmod.machine_id()
    assert a == b                                   # stable
    assert "/" not in a and ":" not in a and " " not in a


def test_runtime_paths_are_under_machine_dir(cfg):
    for p in (cfg.daemon_state_file, cfg.daemon_pid_file, cfg.daemon_port_file,
              cfg.daemon_log_file, cfg.watch_heartbeat_file, cfg.daemon_start_lock):
        assert cfg.machine_dir in p.parents


def test_two_machines_get_isolated_dirs(cfg, monkeypatch):
    monkeypatch.setattr(cfgmod.platform, "node", lambda: "machine-A")
    a = cfg.daemon_state_file
    monkeypatch.setattr(cfgmod.platform, "node", lambda: "machine-B")
    b = cfg.daemon_state_file
    assert a != b                                   # no collision across machines
    assert a.parent != b.parent


def test_shared_index_lock_stays_in_mb_root(cfg):
    # the rebuild lock guards the SHARED index, so it must NOT be per-machine
    assert cfg.lock_dir.parent == cfg.mb_dir


def test_stop_refuses_foreign_machine_daemon(cfg, monkeypatch):
    killed = []
    monkeypatch.setattr(cfgmod.platform, "node", lambda: "this-machine")
    proc.write_json_atomic(cfg.daemon_state_file,
                           {"pid": 4242, "create_time": 1.0, "machine_id": "OTHER-machine"})
    monkeypatch.setattr(proc, "is_running", lambda pid: True)
    monkeypatch.setattr(proc, "kill", lambda pid: killed.append(pid) or True)
    assert client.stop_daemon(cfg) is False
    assert killed == []                             # foreign machine's PID untouched


def test_stop_acts_on_own_machine_daemon(cfg, monkeypatch):
    killed = []
    monkeypatch.setattr(cfgmod.platform, "node", lambda: "this-machine")
    proc.write_json_atomic(cfg.daemon_state_file,
                           {"pid": 4242, "create_time": 1.0, "machine_id": "this-machine"})
    monkeypatch.setattr(proc, "is_running", lambda pid: True)
    monkeypatch.setattr(proc, "pid_create_time", lambda pid: 1.0)
    monkeypatch.setattr(proc, "kill", lambda pid: killed.append(pid) or True)
    monkeypatch.setattr(client.time, "sleep", lambda *_a: None)
    assert client.stop_daemon(cfg) is True
    assert killed == [4242]
