"""Lazy wrapper around the SentenceTransformer model (warm in the daemon)."""
from __future__ import annotations

from pathlib import Path

import numpy as np

_probed_device: str | None = None


def resolve_device(explicit: str | None = None) -> str:
    """Pick the torch device to embed on.

    `torch.cuda.is_available()` is NOT sufficient: a driver/toolkit mismatch
    (a GPU whose compute capability the installed torch wasn't built for, e.g.
    sm_120 against a cu126 wheel) reports the device as available and then
    fails at the first kernel launch with "no kernel image is available for
    execution on the device" — which crashed the whole daemon rather than
    degrading. So the probe actually LAUNCHES a kernel once per process and
    falls back to CPU on any failure. Embedding a MiniLM on CPU is the
    documented baseline speed for this project anyway.

    An explicit `encoder_device` config value always wins and is never probed —
    if you ask for cuda you get cuda, including its errors."""
    global _probed_device
    if explicit:
        return str(explicit)
    if _probed_device is not None:
        return _probed_device
    device = "cpu"
    try:
        import torch

        if torch.cuda.is_available():
            (torch.zeros(1, device="cuda") + 1).cpu()   # real kernel launch + sync
            device = "cuda"
    except Exception:  # noqa: BLE001 — any CUDA failure means "use the CPU"
        device = "cpu"
    _probed_device = device
    return device


class Encoder:
    def __init__(self, model_name: str, batch_size: int = 64,
                 model_path: str | Path | None = None,
                 device: str | None = None):
        self.model_name = model_name
        self.batch_size = batch_size
        self.model_path = Path(model_path).resolve() if model_path is not None else None
        self.device = device
        self._model = None

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # heavy import

            if self.model_path is None:
                from .model_store import ModelNotInstalledError
                raise ModelNotInstalledError(
                    f"embedding model {self.model_name!r} has no local snapshot; "
                    "run `mb model install` or `mb doctor --fix --heavy`")
            self._model = SentenceTransformer(
                str(self.model_path), local_files_only=True,
                device=resolve_device(self.device))
        return self._model

    def encode(self, texts: list[str], progress: bool = False) -> np.ndarray:
        model = self._load()
        emb = model.encode(
            texts,
            batch_size=self.batch_size,
            show_progress_bar=progress,
            convert_to_numpy=True,
            normalize_embeddings=True,   # cosine == dot product on normalized vectors
        )
        return np.asarray(emb, dtype=np.float32)


_shared: Encoder | None = None


def shared_encoder(model_name: str, batch_size: int = 64,
                   model_path: str | Path | None = None, *, cfg=None) -> Encoder:
    """Process-wide encoder (daemon and cold fallback reuse the same instance)."""
    global _shared
    if model_path is None and cfg is not None:
        from .model_store import require_snapshot
        model_path = require_snapshot(cfg)
    resolved = Path(model_path).resolve() if model_path is not None else None
    device = cfg.get("encoder_device") if cfg is not None else None
    if (_shared is None or _shared.model_name != model_name
            or _shared.model_path != resolved or _shared.device != device):
        _shared = Encoder(model_name, batch_size, resolved, device)
    return _shared
