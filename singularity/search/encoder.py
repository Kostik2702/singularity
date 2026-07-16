"""Lazy wrapper around the SentenceTransformer model (warm in the daemon)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np


class Encoder:
    def __init__(self, model_name: str, batch_size: int = 64,
                 model_path: str | Path | None = None):
        self.model_name = model_name
        self.batch_size = batch_size
        self.model_path = Path(model_path).resolve() if model_path is not None else None
        self._model = None

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # heavy import

            if self.model_path is None:
                from .model_store import ModelNotInstalledError
                raise ModelNotInstalledError(
                    f"embedding model {self.model_name!r} has no local snapshot; "
                    "run `mb model install` or `mb doctor --fix --heavy`")
            self._model = SentenceTransformer(str(self.model_path), local_files_only=True)
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


_shared: Optional[Encoder] = None


def shared_encoder(model_name: str, batch_size: int = 64,
                   model_path: str | Path | None = None, *, cfg=None) -> Encoder:
    """Process-wide encoder (daemon and cold fallback reuse the same instance)."""
    global _shared
    if model_path is None and cfg is not None:
        from .model_store import require_snapshot
        model_path = require_snapshot(cfg)
    resolved = Path(model_path).resolve() if model_path is not None else None
    if (_shared is None or _shared.model_name != model_name
            or _shared.model_path != resolved):
        _shared = Encoder(model_name, batch_size, resolved)
    return _shared
