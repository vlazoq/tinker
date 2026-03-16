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

import math
import re
from abc import ABC, abstractmethod
from collections import Counter
from typing import List, Optional


# ─────────────────────────────────────────────────────────────
# Abstract interface
# ─────────────────────────────────────────────────────────────

class EmbeddingBackend(ABC):
    @abstractmethod
    def embed(self, text: str) -> List[float]:
        """Return a unit-length embedding vector for *text*."""
        ...

    # ── shared utility ──────────────────────────────────────

    @staticmethod
    def cosine_similarity(a: List[float], b: List[float]) -> float:
        """Cosine similarity between two pre-normalised vectors."""
        if len(a) != len(b):
            raise ValueError("Vector dimension mismatch")
        dot = sum(x * y for x, y in zip(a, b))
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

    def embed(self, text: str) -> List[float]:
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
    def _normalise(v: List[float]) -> List[float]:
        magnitude = math.sqrt(sum(x * x for x in v))
        if magnitude == 0.0:
            return v
        return [x / magnitude for x in v]


# ─────────────────────────────────────────────────────────────
# TF-IDF fallback backend (no dependencies, deterministic)
# ─────────────────────────────────────────────────────────────

class FallbackTFIDFBackend(EmbeddingBackend):
    """
    Very lightweight TF-IDF bag-of-words embedding.
    Suitable for testing and environments without Ollama.
    Vocabulary is built incrementally from all embedded texts.

    Dimensions = size of vocabulary (capped at max_vocab).
    Vectors are L2-normalised before returning.
    """

    def __init__(self, max_vocab: int = 512):
        self.max_vocab: List[str] = []
        self.max_vocab_size = max_vocab
        self._doc_freq: Counter = Counter()
        self._doc_count: int = 0
        self._tokenise = lambda t: re.findall(r"[a-z]+", t.lower())

    # ── vocabulary management ────────────────────────────────

    def _update_vocab(self, tokens: List[str]) -> None:
        self._doc_count += 1
        for tok in set(tokens):
            self._doc_freq[tok] += 1
        if len(self.max_vocab) < self.max_vocab_size:
            for tok in tokens:
                if tok not in self.max_vocab:
                    self.max_vocab.append(tok)
                    if len(self.max_vocab) == self.max_vocab_size:
                        break

    def _tfidf_vector(self, tokens: List[str]) -> List[float]:
        if not self.max_vocab:
            return [0.0]
        tf = Counter(tokens)
        total = max(len(tokens), 1)
        vec: List[float] = []
        for word in self.max_vocab:
            tf_val = tf[word] / total
            df = self._doc_freq.get(word, 0)
            idf = math.log((self._doc_count + 1) / (df + 1)) + 1.0
            vec.append(tf_val * idf)
        return vec

    @staticmethod
    def _l2_normalise(v: List[float]) -> List[float]:
        mag = math.sqrt(sum(x * x for x in v))
        if mag == 0.0:
            return v
        return [x / mag for x in v]

    def embed(self, text: str) -> List[float]:
        tokens = self._tokenise(text)
        self._update_vocab(tokens)
        raw = self._tfidf_vector(tokens)
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
        return FallbackTFIDFBackend()
    try:
        import urllib.request
        with urllib.request.urlopen(
            f"{ollama_url}/api/tags", timeout=3
        ) as resp:
            if resp.status == 200:
                return OllamaEmbeddingBackend(model=model, base_url=ollama_url)
    except Exception:
        pass
    return FallbackTFIDFBackend()
