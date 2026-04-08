"""
tinker/anti_stagnation/embeddings.py
──────────────────────────────────────
Lightweight embedding backend.

The production path calls Ollama's /api/embeddings endpoint.
A FallbackTFIDFBackend is provided so tests and offline environments
work without a running Ollama instance.

Design decisions:
  - The interface is synchronous; the caller (detector) is responsible
    for threading/async if needed.
  - Embeddings are NOT cached here — the SemanticLoopDetector keeps its
    own sliding-window cache of vectors.
"""

from __future__ import annotations

import logging
import math
import re
from abc import ABC, abstractmethod
from collections import Counter

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Abstract interface
# ─────────────────────────────────────────────────────────────


class EmbeddingBackend(ABC):
    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Return a unit-length embedding vector for *text*."""
        ...

    # ── shared utility ──────────────────────────────────────

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        """Cosine similarity between two pre-normalised vectors."""
        if len(a) != len(b):
            raise ValueError("Vector dimension mismatch")
        dot = sum(x * y for x, y in zip(a, b, strict=False))
        # Clamp for floating-point drift
        return max(-1.0, min(1.0, dot))


# ─────────────────────────────────────────────────────────────
# Ollama backend (production)
# ─────────────────────────────────────────────────────────────


class OllamaEmbeddingBackend(EmbeddingBackend):
    """
    Calls the local Ollama /api/embeddings endpoint.
    Requires: pip install requests
    """

    def __init__(
        self,
        model: str = "nomic-embed-text",
        base_url: str = "http://localhost:11434",
        timeout: int = 30,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._requests = None  # lazy import

    def _get_requests(self):
        if self._requests is None:
            import requests  # type: ignore

            self._requests = requests
        return self._requests

    def embed(self, text: str) -> list[float]:
        requests = self._get_requests()
        response = requests.post(
            f"{self.base_url}/api/embeddings",
            json={"model": self.model, "prompt": text},
            timeout=self.timeout,
        )
        response.raise_for_status()
        raw = response.json()["embedding"]
        return self._normalise(raw)

    @staticmethod
    def _normalise(v: list[float]) -> list[float]:
        magnitude = math.sqrt(sum(x * x for x in v))
        if magnitude == 0.0:
            return v
        return [x / magnitude for x in v]


# ─────────────────────────────────────────────────────────────
# TF-IDF fallback backend (no dependencies, deterministic)
# ─────────────────────────────────────────────────────────────


class FallbackTFIDFBackend(EmbeddingBackend):
    """
    Very lightweight fixed-dimension hashing-trick embedding.
    Suitable for testing and environments without Ollama.

    Uses a fixed number of buckets (``dim``) so every call to ``embed()``
    returns a vector of exactly the same length — no vocabulary growth and
    therefore no dimension-mismatch errors when comparing old and new
    embeddings with ``cosine_similarity()``.

    Vectors are L2-normalised before returning.
    """

    def __init__(self, dim: int = 256):
        self._dim = dim
        self._tokenise = lambda t: re.findall(r"[a-z]+", t.lower())

    # ── hashing-trick vector ─────────────────────────────────

    def _hash_vector(self, tokens: list[str]) -> list[float]:
        vec = [0.0] * self._dim
        tf: Counter = Counter(tokens)
        total = max(len(tokens), 1)
        for tok, count in tf.items():
            # Two independent hashes: one for bucket, one for sign
            bucket = hash(tok) % self._dim
            sign = 1.0 if hash(tok + "_sign") % 2 == 0 else -1.0
            vec[bucket] += sign * (count / total)
        return vec

    @staticmethod
    def _l2_normalise(v: list[float]) -> list[float]:
        mag = math.sqrt(sum(x * x for x in v))
        if mag == 0.0:
            return v
        return [x / mag for x in v]

    def embed(self, text: str) -> list[float]:
        tokens = self._tokenise(text)
        raw = self._hash_vector(tokens)
        return self._l2_normalise(raw)


# ─────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────


def make_embedding_backend(
    model: str = "nomic-embed-text",
    ollama_url: str = "http://localhost:11434",
    force_fallback: bool = False,
) -> EmbeddingBackend:
    """
    Returns an OllamaEmbeddingBackend if Ollama is reachable,
    otherwise falls back to FallbackTFIDFBackend.
    Set force_fallback=True in tests.
    """
    if force_fallback:
        logger.debug("Embedding backend: using TF-IDF fallback (force_fallback=True)")
        return FallbackTFIDFBackend()
    try:
        import urllib.request

        with urllib.request.urlopen(f"{ollama_url}/api/tags", timeout=3) as resp:
            if resp.status == 200:
                return OllamaEmbeddingBackend(model=model, base_url=ollama_url)
    except Exception:
        logger.warning(
            "Ollama not reachable at %s — falling back to TF-IDF embedding backend",
            ollama_url,
        )
    return FallbackTFIDFBackend()
