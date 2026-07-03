"""Lazy wrapper around the SentenceTransformer model (warm in the daemon)."""
from __future__ import annotations

from typing import Optional

import numpy as np


class Encoder:
    def __init__(self, model_name: str, batch_size: int = 64):
        self.model_name = model_name
        self.batch_size = batch_size
        self._model = None

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # heavy import

            self._model = SentenceTransformer(self.model_name)
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


def shared_encoder(model_name: str, batch_size: int = 64) -> Encoder:
    """Process-wide encoder (daemon and cold fallback reuse the same instance)."""
    global _shared
    if _shared is None or _shared.model_name != model_name:
        _shared = Encoder(model_name, batch_size)
    return _shared
