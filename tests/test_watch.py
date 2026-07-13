"""T12: the watcher must not silently drop pending files when the index
writer lock is held (e.g. `mb nightly` running) or a batch update fails --
see [[SINGULARITY_INCOMING_FORK_IMPLEMENTATION]] T12, AI prompt clause
"при lock не теряй pending watcher files"."""
import pytest

from singularity import watch
from singularity.config import Config
from singularity.search import indexer


@pytest.fixture
def cfg(tmp_path):
    return Config(vault=tmp_path, data={})


def test_locked_index_requeues_pending_files(cfg, monkeypatch):
    monkeypatch.setattr(indexer, "update",
                        lambda cfg, only_files=None: (_ for _ in ()).throw(
                            indexer.IndexLockedError("locked")))
    pending = {"a.md": 1.0}
    watch._process_pending(cfg, pending)
    assert "a.md" in pending


def test_generic_failure_requeues_pending_files(cfg, monkeypatch):
    monkeypatch.setattr(indexer, "update",
                        lambda cfg, only_files=None: (_ for _ in ()).throw(RuntimeError("boom")))
    pending = {"a.md": 1.0}
    watch._process_pending(cfg, pending)
    assert "a.md" in pending


def test_requeued_files_get_a_fresh_timestamp(cfg, monkeypatch):
    monkeypatch.setattr(indexer, "update",
                        lambda cfg, only_files=None: (_ for _ in ()).throw(
                            indexer.IndexLockedError("locked")))
    pending = {"a.md": 1.0}
    watch._process_pending(cfg, pending)
    assert pending["a.md"] > 1.0


def test_success_clears_processed_files(cfg, monkeypatch):
    import singularity.search.client as client_mod
    monkeypatch.setattr(indexer, "update", lambda cfg, only_files=None: {"added": 1})
    monkeypatch.setattr(client_mod, "reload_daemon", lambda cfg: None)
    pending = {"a.md": 1.0}
    watch._process_pending(cfg, pending)
    assert pending == {}


def test_success_preserves_files_added_during_update(cfg, monkeypatch):
    import singularity.search.client as client_mod
    pending = {"a.md": 1.0}

    def fake_update(cfg, only_files=None):
        pending["b.md"] = 999.0   # observer thread marks a NEW file mid-update
        return {"added": 1}
    monkeypatch.setattr(indexer, "update", fake_update)
    monkeypatch.setattr(client_mod, "reload_daemon", lambda cfg: None)
    watch._process_pending(cfg, pending)
    assert "a.md" not in pending    # processed, cleared
    assert "b.md" in pending        # arrived mid-update, preserved for the next batch
