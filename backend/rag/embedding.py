"""Embedding providers for PaperLens hybrid RAG."""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Protocol

import numpy as np
from django.conf import settings

logger = logging.getLogger(__name__)

QUERY_INSTRUCTION = "Represent this research question for retrieving relevant CS paper passages."
DOCUMENT_INSTRUCTION = "Represent this CS paper passage for retrieval."

_embedder = None
_provider = None


class EmbeddingProvider(Protocol):
    model_name: str
    dimension: int
    version: str

    def encode(self, texts: list[str], *, input_type: str = "document") -> np.ndarray:
        ...


@dataclass
class Qwen3LocalEmbeddingProvider:
    """SentenceTransformers-backed Qwen3 embedding provider."""

    model_name: str
    dimension: int = 1024
    version: str = ""

    def __post_init__(self) -> None:
        if not self.version:
            self.version = f"{self.model_name}:dim{self.dimension}:norm"

    def encode(self, texts: list[str], *, input_type: str = "document") -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dimension), dtype=np.float32)
        model = get_embedder(self.model_name)
        prepared = [_with_instruction(text, input_type) for text in texts]
        logger.info(
            "embedding batch started",
            extra={
                "event": "embedding_batch_started",
                "embedding_model": self.model_name,
                "embedding_dim": self.dimension,
                "input_type": input_type,
                "text_count": len(texts),
            },
        )
        vectors = model.encode(prepared, normalize_embeddings=True)
        arr = np.array(vectors, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        if arr.shape[1] != self.dimension:
            logger.warning(
                "embedding dimension differs from configured index",
                extra={
                    "event": "embedding_dimension_mismatch",
                    "embedding_model": self.model_name,
                    "configured_dim": self.dimension,
                    "actual_dim": int(arr.shape[1]),
                    "status": "warning",
                },
            )
        return arr


@dataclass
class FakeEmbeddingProvider:
    """Deterministic local provider for tests and no-network demos."""

    model_name: str = "fake-hash-embedding"
    dimension: int = 1024
    version: str = "fake-hash-embedding:v1"

    def encode(self, texts: list[str], *, input_type: str = "document") -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dimension), dtype=np.float32)
        rows = []
        for text in texts:
            rows.append(_hash_embedding(f"{input_type}:{text}", self.dimension))
        return np.array(rows, dtype=np.float32)


def get_embedder(model_name: str | None = None):
    """Return the lazily loaded SentenceTransformer model.

    Tests patch this function directly, so it intentionally remains part of the
    public module API.
    """

    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer

        resolved = model_name or _setting("PAPERLENS_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-0.6B")
        logger.info(
            "loading embedding model",
            extra={"event": "embedding_model_load_started", "embedding_model": resolved},
        )
        _embedder = SentenceTransformer(resolved)
    return _embedder


def get_provider() -> EmbeddingProvider:
    global _provider
    if _provider is not None:
        return _provider
    provider_name = str(_setting("PAPERLENS_EMBEDDING_PROVIDER", "qwen3-local")).lower()
    dim = int(_setting("PAPERLENS_EMBEDDING_DIM", 1024))
    if provider_name in {"fake", "fake-hash", "test"}:
        _provider = FakeEmbeddingProvider(dimension=dim)
    else:
        model_name = str(_setting("PAPERLENS_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-0.6B"))
        version = str(
            _setting(
                "PAPERLENS_EMBEDDING_VERSION",
                f"{model_name}:dim{dim}:norm",
            )
        )
        _provider = Qwen3LocalEmbeddingProvider(
            model_name=model_name,
            dimension=dim,
            version=version,
        )
    return _provider


def embed(texts: list[str], *, input_type: str = "document") -> np.ndarray:
    """Encode texts as normalized vectors."""

    return get_provider().encode(texts, input_type=input_type)


def embedding_metadata() -> dict[str, str | int]:
    provider = get_provider()
    return {
        "embedding_model": provider.model_name,
        "embedding_dim": provider.dimension,
        "embedding_version": provider.version,
    }


def reset_embedding_provider() -> None:
    global _provider, _embedder
    _provider = None
    _embedder = None


def _with_instruction(text: str, input_type: str) -> str:
    instruction = QUERY_INSTRUCTION if input_type == "query" else DOCUMENT_INSTRUCTION
    return f"{instruction}\n{text}"


def _hash_embedding(text: str, dimension: int) -> np.ndarray:
    vector = np.zeros(dimension, dtype=np.float32)
    tokens = [token for token in text.lower().replace("/", " ").replace("-", " ").split() if token]
    if not tokens:
        tokens = [text.lower() or "empty"]
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        for offset in range(0, len(digest), 4):
            index = int.from_bytes(digest[offset : offset + 4], "little") % dimension
            vector[index] += 1.0
    norm = float(np.linalg.norm(vector))
    if norm > 0:
        vector /= norm
    return vector


def _setting(name: str, default):
    if settings.configured:
        return getattr(settings, name, os.environ.get(name, default))
    return os.environ.get(name, default)
