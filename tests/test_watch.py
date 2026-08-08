"""T12: the watcher must not silently drop pending files when the index
writer lock is held (e.g. `mb nightly` running) or a batch update fails --
see [[SINGULARITY_INCOMING_FORK_IMPLEMENTATION]] T12, AI prompt clause
"do not lose pending watcher files when the lock is held"."""
import pytest

from singularity.machine import watch
from singularity.config import Config
from singularity.search import client


@pytest.fixture
def cfg(tmp_path):
    return Config(vault=tmp_path, data={})


def test_locked_index_requeues_pending_files(cfg, monkeypatch):
    monkeypatch.setattr(client, "run_index",
                        lambda cfg, action, files=None: (_ for _ in ()).throw(
                            client.RemoteIndexError("locked", error_type="IndexLockedError")))
    pending = {"a.md": 1.0}
    watch._process_pending(cfg, pending)
    assert "a.md" in pending


def test_generic_failure_requeues_pending_files(cfg, monkeypatch):
    monkeypatch.setattr(client, "run_index",
                        lambda cfg, action, files=None: (_ for _ in ()).throw(RuntimeError("boom")))
    pending = {"a.md": 1.0}
    watch._process_pending(cfg, pending)
    assert "a.md" in pending


def test_requeued_files_get_a_fresh_timestamp(cfg, monkeypatch):
    monkeypatch.setattr(client, "run_index",
                        lambda cfg, action, files=None: (_ for _ in ()).throw(
                            client.RemoteIndexError("locked", error_type="IndexLockedError")))
    pending = {"a.md": 1.0}
    watch._process_pending(cfg, pending)
    assert pending["a.md"] > 1.0


def test_success_clears_processed_files(cfg, monkeypatch):
    monkeypatch.setattr(client, "run_index", lambda cfg, action, files=None: {"added": 1})
    pending = {"a.md": 1.0}
    watch._process_pending(cfg, pending)
    assert pending == {}


def test_success_preserves_files_added_during_update(cfg, monkeypatch):
    pending = {"a.md": 1.0}

    def fake_update(cfg, action, files=None):
        pending["b.md"] = 999.0   # observer thread marks a NEW file mid-update
        return {"added": 1}
    monkeypatch.setattr(client, "run_index", fake_update)
    watch._process_pending(cfg, pending)
    assert "a.md" not in pending    # processed, cleared
    assert "b.md" in pending        # arrived mid-update, preserved for the next batch
