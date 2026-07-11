"""AUD-21: mb stats analytics (metadata only, no ML)."""
import json

import numpy as np
import pytest

from singularity import stats
from singularity.config import Config
from singularity.search import indexer


def _fake_encoder(*a, **k):
    return type("E", (), {"encode": staticmethod(
        lambda texts, progress=False: np.ones((len(texts), 4), dtype=np.float32))})()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer, "shared_encoder", _fake_encoder)
    vault = tmp_path / "v"
    (vault / "howto").mkdir(parents=True)
    (vault / "decisions").mkdir(parents=True)
    (vault / "howto" / "a.md").write_text(
        "# A\n\n## S\n" + "meaningful howto content here " * 5, encoding="utf-8")
    (vault / "decisions" / "b.md").write_text(
        "# B\n\n## S\n" + "meaningful decision content here " * 5, encoding="utf-8")
    return Config(vault=vault, data={"model": "fake"})


def test_stats_absent_index(cfg):
    s = stats.compute(cfg)
    assert s["present"] is False
    assert s["vault_notes"] == 2
    assert s["unindexed"] == 2          # nothing indexed yet


def test_stats_after_rebuild(cfg):
    indexer.rebuild(cfg, progress=False)
    s = stats.compute(cfg)
    assert s["present"] is True
    assert s["notes"] == 2
    assert s["chunks"] >= 2
    assert set(s["by_type"]) == {"howto", "decision"}
    assert s["chars_indexed"] > 0
    assert s["unindexed"] == 0
    assert s["vault_notes"] == 2


def test_stats_counts_duplicate_chunks(cfg):
    # two files with identical section content -> a duplicate chunk
    (cfg.vault / "howto" / "dup.md").write_text(
        "# A\n\n## S\n" + "meaningful howto content here " * 5, encoding="utf-8")
    indexer.rebuild(cfg, progress=False)
    s = stats.compute(cfg)
    assert s["duplicate_chunks"] >= 1


def test_stats_json_output(cfg, capsys):
    indexer.rebuild(cfg, progress=False)
    stats.run(cfg, as_json=True)
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["notes"] == 2


def test_stats_never_emits_content(cfg, capsys):
    indexer.rebuild(cfg, progress=False)
    stats.run(cfg, as_json=True)
    out = capsys.readouterr().out
    assert "meaningful" not in out          # counts only, no note content
