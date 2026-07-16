import json
from pathlib import Path

import pytest

from singularity.config import Config
from singularity.search import model_store


def _cfg(tmp_path):
    return Config(vault=tmp_path, data={"hf_cache": str(tmp_path / "hf")})


def _snapshot(root: Path, revision: str = "abc123") -> Path:
    snap = root / "models--owner--model" / "snapshots" / revision
    snap.mkdir(parents=True)
    for rel in ("modules.json", "config.json", "model.safetensors"):
        (snap / rel).write_text("ok", encoding="utf-8")
    return snap


def test_install_adopts_complete_local_snapshot_without_network(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    snap = _snapshot(cfg.hf_cache)
    calls = []

    def fake_download(**kwargs):
        calls.append(kwargs["local_files_only"])
        assert kwargs["local_files_only"] is True
        return str(snap)

    monkeypatch.setattr(model_store, "_snapshot_download", fake_download)
    result = model_store.install(cfg)

    assert calls == [True]
    assert result["revision"] == "abc123"
    manifest = json.loads(model_store.manifest_path(cfg).read_text(encoding="utf-8"))
    assert manifest["model"] == cfg.model
    assert model_store.require_snapshot(cfg) == snap.resolve()


def test_install_downloads_only_in_explicit_install_path(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    snap = _snapshot(cfg.hf_cache, "downloaded")
    calls = []

    def fake_download(**kwargs):
        calls.append(kwargs["local_files_only"])
        if kwargs["local_files_only"]:
            raise OSError("not cached")
        return str(snap)

    monkeypatch.setattr(model_store, "_snapshot_download", fake_download)
    result = model_store.install(cfg)
    assert calls == [True, False]
    assert result["revision"] == "downloaded"


def test_runtime_missing_manifest_fails_fast_without_hub(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(model_store, "_snapshot_download",
                        lambda **kwargs: pytest.fail("runtime touched Hugging Face"))
    with pytest.raises(model_store.ModelNotInstalledError, match="mb model install"):
        model_store.require_snapshot(cfg)


def test_status_rejects_incomplete_snapshot(tmp_path):
    cfg = _cfg(tmp_path)
    snap = cfg.hf_cache / "snapshots" / "broken"
    snap.mkdir(parents=True)
    model_store.write_manifest(cfg, snap, "broken")
    status = model_store.status(cfg)
    assert status["installed"] is False
    assert "missing" in status["reason"]
