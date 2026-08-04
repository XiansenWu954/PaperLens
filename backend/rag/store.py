"""Small NumPy fallback store used when Postgres vector search is unavailable."""
from __future__ import annotations

import logging

import numpy as np
from django.conf import settings

from .models import Text

logger = logging.getLogger(__name__)


class NumpyVectorStore:
    def __init__(self) -> None:
        self.matrix: np.ndarray | None = None
        self.texts: list[Text] = []

    def build_from(self, texts: list[Text]) -> None:
        if not texts:
            dim = int(getattr(settings, "PAPERLENS_EMBEDDING_DIM", 1024))
            self.matrix = np.zeros((0, dim), dtype=np.float32)
            self.texts = []
            return
        self.matrix = np.array([t.embedding for t in texts], dtype=np.float32)
        self.texts = texts
        logger.info(
            "numpy vector store built",
            extra={"event": "numpy_vector_store_built", "text_count": len(texts)},
        )

    def search(self, query_vec: np.ndarray, k: int = 20) -> list[Text]:
        if self.matrix is None or len(self.texts) == 0:
            return []
        if self.matrix.shape[1] != query_vec.shape[0]:
            logger.warning(
                "numpy vector dimensions differ",
                extra={
                    "event": "numpy_vector_dimension_mismatch",
                    "matrix_dim": int(self.matrix.shape[1]),
                    "query_dim": int(query_vec.shape[0]),
                    "status": "warning",
                },
            )
            return []
        sims = self.matrix @ query_vec
        k = min(k, len(self.texts))
        idx = np.argsort(sims)[::-1][:k]
        return [self.texts[i] for i in idx]
