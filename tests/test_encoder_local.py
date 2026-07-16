import sys
import types

from singularity.search import encoder


def test_encoder_loads_snapshot_offline_once(tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    calls = []

    class FakeModel:
        def __init__(self, model_path, **kwargs):
            calls.append((model_path, kwargs))

        def encode(self, texts, **kwargs):
            import numpy as np
            return np.ones((len(texts), 2), dtype=np.float32)

    monkeypatch.setitem(sys.modules, "sentence_transformers",
                        types.SimpleNamespace(SentenceTransformer=FakeModel))
    enc = encoder.Encoder("owner/model", model_path=snapshot)
    enc.encode(["one"])
    enc.encode(["two"])

    assert len(calls) == 1
    assert calls[0][0] == str(snapshot.resolve())
    assert calls[0][1]["local_files_only"] is True


def test_shared_encoder_key_includes_snapshot_path(tmp_path):
    encoder._shared = None
    first = encoder.shared_encoder("owner/model", model_path=tmp_path / "one")
    same = encoder.shared_encoder("owner/model", model_path=tmp_path / "one")
    other = encoder.shared_encoder("owner/model", model_path=tmp_path / "two")
    assert first is same
    assert other is not first
