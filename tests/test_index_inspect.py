"""AUD-17: mb index inspect — chunk-level view of one indexed file."""
import numpy as np

from singularity.config import Config
from singularity.search import indexer


def test_inspect_returns_chunks_for_file(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer, "shared_encoder",
                        lambda *a, **k: type("E", (), {"encode": staticmethod(
                            lambda texts, progress=False: np.ones((len(texts), 4),
                                                                  dtype=np.float32))})())
    vault = tmp_path / "v"; (vault / "howto").mkdir(parents=True)
    (vault / "howto" / "a.md").write_text(
        "# A\n\n## Sec\n" + "meaningful content here " * 5, encoding="utf-8")
    cfg = Config(vault=vault, data={"model": "fake"})
    indexer.rebuild(cfg, progress=False)
    rows = indexer.inspect(cfg, "howto/a.md")
    assert rows and all("chunk_id" in r and "chars" in r for r in rows)
    assert indexer.inspect(cfg, "nope/x.md") == []
