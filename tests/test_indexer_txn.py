"""AUD-04: transactional index generations + single writer lock.

Guarantees:
- a reader only ever sees a whole, committed generation;
- a crash mid-commit never destroys the last-good generation;
- rebuild/update/watcher writers are serialized by one lock;
- the legacy flat layout stays readable until the next rebuild migrates it.
"""
import json

import numpy as np
import pytest

from singularity.config import Config
from singularity.search import indexer


@pytest.fixture
def cfg(tmp_path):
    return Config(vault=tmp_path, data={})


def _emb(n, dim=4):
    return np.ones((n, dim), dtype=np.float32)


def _meta(n):
    return {"model": "m", "file_meta": {}, "chunks": [{"file": "a", "text": "t"}] * n}


def _commit(index_dir, embs, meta):
    """C01 made `chunk_schema` a required keyword-only argument so no writer can
    silently inherit the markdown chunk format (D3). These tests exercise the
    transactional layer, not the chunk shape, so they pin it once here."""
    indexer._commit_generation(index_dir, embs, meta,
                               chunk_schema=indexer.chunking.CHUNK_SCHEMA_VERSION)


# --- commit + read -----------------------------------------------------------

def test_commit_then_read_current(cfg):
    d = cfg.memory_index_dir
    _commit(d, _emb(3), _meta(3))
    files = indexer.index_files(d)
    assert files is not None
    emb_path, meta_path = files
    assert np.load(emb_path).shape[0] == 3
    assert json.loads(meta_path.read_text(encoding="utf-8"))["chunk_count"] == 3


def test_commit_refuses_torn_generation(cfg):
    # embeddings rows must match chunk count — never commit a mismatched pair
    with pytest.raises(ValueError):
        _commit(cfg.memory_index_dir, _emb(2), _meta(5))


def test_second_commit_supersedes_and_prunes(cfg):
    d = cfg.memory_index_dir
    _commit(d, _emb(1), _meta(1))
    _commit(d, _emb(2), _meta(2))
    _commit(d, _emb(3), _meta(3))
    emb_path, _ = indexer.index_files(d)
    assert np.load(emb_path).shape[0] == 3           # newest is current
    gens = list(d.glob("gen_*"))
    assert len(gens) <= 2                            # keeps current + one last-good


# --- crash safety ------------------------------------------------------------

def test_crash_before_pointer_swap_keeps_last_good(cfg, monkeypatch):
    d = cfg.memory_index_dir
    _commit(d, _emb(1), _meta(1))     # last-good = 1 row

    # simulate a crash: the new generation's files get written, but the process
    # dies before the CURRENT pointer is swapped.
    import os
    real_replace = os.replace

    def boom(src, dst):
        if str(dst).endswith("CURRENT"):
            raise KeyboardInterrupt("crash during commit")
        return real_replace(src, dst)

    monkeypatch.setattr(indexer.os, "replace", boom)
    with pytest.raises(KeyboardInterrupt):
        _commit(d, _emb(9), _meta(9))
    monkeypatch.undo()

    emb_path, _ = indexer.index_files(d)
    assert np.load(emb_path).shape[0] == 1               # last-good intact, not the 9-row


def test_same_millisecond_commits_do_not_collide(cfg, monkeypatch):
    """Regression: two commits within the SAME millisecond in one process used to
    mint an identical gen token and collapse onto one directory, silently
    overwriting the immutable last-good generation. This deterministically forces
    that timing (it was a flaky POSIX-only CI failure before the sequence counter).
    """
    from unittest import mock
    d = cfg.memory_index_dir
    with mock.patch.object(indexer.time, "time", return_value=1000.0):
        _commit(d, _emb(1), _meta(1))     # last-good = 1 row

        real_replace = indexer.os.replace

        def boom(src, dst):
            if str(dst).endswith("CURRENT"):
                raise KeyboardInterrupt("crash during commit")
            return real_replace(src, dst)

        monkeypatch.setattr(indexer.os, "replace", boom)
        with pytest.raises(KeyboardInterrupt):
            _commit(d, _emb(9), _meta(9))  # same ms, must NOT collide
        monkeypatch.undo()

    assert len(list(d.glob("gen_*"))) == 2               # two distinct generations
    emb_path, _ = indexer.index_files(d)
    assert np.load(emb_path).shape[0] == 1               # last-good survived the crash


def test_reader_ignores_half_written_current_falls_back(cfg):
    d = cfg.memory_index_dir
    _commit(d, _emb(4), _meta(4))
    good = (d / "CURRENT").read_text(encoding="utf-8").strip()
    # point CURRENT at a torn/nonexistent generation
    (d / "CURRENT").write_text("gen_does_not_exist", encoding="utf-8")
    emb_path, _ = indexer.index_files(d)                 # falls back to newest whole gen
    assert np.load(emb_path).shape[0] == 4
    assert good  # sanity


def test_legacy_flat_layout_is_readable(cfg):
    d = cfg.memory_index_dir
    d.mkdir(parents=True, exist_ok=True)
    np.save(d / "embeddings.npy", _emb(2), allow_pickle=False)
    (d / "metadata.json").write_text(json.dumps(_meta(2)), encoding="utf-8")
    files = indexer.index_files(d)
    assert files is not None
    assert np.load(files[0]).shape[0] == 2


def test_no_index_returns_none(cfg):
    assert indexer.index_files(cfg.memory_index_dir) is None


# --- writer lock -------------------------------------------------------------

def test_writer_lock_is_exclusive(cfg):
    # The inner acquisition must raise INSIDE the raises-context; merging the
    # `with` statements would change what is being asserted (SIM117).
    with indexer._writer_lock(cfg), pytest.raises(indexer.IndexLockedError):  # noqa: SIM117
        with indexer._writer_lock(cfg):  # noqa: SIM117
            pass
    # released afterwards -> can acquire again
    with indexer._writer_lock(cfg):
        pass


def test_losing_contender_does_not_delete_the_winners_lock(cfg):
    # T12 real-machine finding: a contended acquisition attempt used to run
    # `finally: shutil.rmtree(lock)` unconditionally, deleting the OTHER
    # writer's still-active lock the instant its own `mkdir` failed -- which
    # would let a THIRD writer race in while the real holder was mid-write.
    with indexer._writer_lock(cfg):
        assert cfg.lock_dir.exists()
        with pytest.raises(indexer.IndexLockedError), indexer._writer_lock(cfg):
            pass
        assert cfg.lock_dir.exists()   # still held by the outer (winning) writer
    assert not cfg.lock_dir.exists()   # released once the actual owner exits


def test_writer_lock_takes_over_stale(cfg, monkeypatch):
    cfg.lock_dir.parent.mkdir(parents=True, exist_ok=True)
    cfg.lock_dir.mkdir()
    old = indexer.time.time() - (indexer.STALE_LOCK_SECONDS + 10)
    import os
    os.utime(cfg.lock_dir, (old, old))
    with indexer._writer_lock(cfg):   # stale -> taken over, no raise
        pass
