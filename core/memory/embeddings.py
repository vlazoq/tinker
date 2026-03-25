"""
embeddings.py — Local embedding pipeline using sentence-transformers.

Designed to be:
  - Lazy-loaded (model only hits memory when first used)
  - Thread-safe via asyncio.Lock so concurrent coroutines queue rather than
    each spawning their own model load
  - Batched for efficiency when embedding many documents at once
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class EmbeddingPipeline:
    """
    Async wrapper around a sentence-transformers encoder.

    Usage
    -----
    pipeline = EmbeddingPipeline(model_name="all-MiniLM-L6-v2")
    vector   = await pipeline.embed("some text")
    vectors  = await pipeline.embed_batch(["text1", "text2"])
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2", device: str = "cpu"):
        self.model_name = model_name
        self.device = device
        self._model = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_loaded(self) -> None:
        """Lazy-load the model exactly once, even under concurrent calls."""
        if self._model is not None:
            return
        async with self._lock:
            if self._model is not None:  # double-checked inside lock
                return
            logger.info(
                "Loading embedding model '%s' on %s…", self.model_name, self.device
            )
            loop = asyncio.get_running_loop()
            self._model = await loop.run_in_executor(None, self._load_model)
            logger.info("Embedding model loaded.")

    def _load_model(self):
        """Runs in a thread-pool executor so it doesn't block the event loop."""
        from sentence_transformers import SentenceTransformer  # type: ignore

        return SentenceTransformer(self.model_name, device=self.device)

    def _encode_sync(self, texts: list[str]) -> list[list[float]]:
        """CPU-bound encode — always called via run_in_executor."""
        embeddings = self._model.encode(
            texts,
            batch_size=32,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        return embeddings.tolist()

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def embed(self, text: str) -> list[float]:
        """Embed a single string. Returns a normalised float list."""
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """
        Embed a list of strings in one batched forward pass.
        Runs the CPU-bound encode on a thread-pool executor.
        """
        if not texts:
            return []
        await self._ensure_loaded()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._encode_sync, texts)

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    async def warmup(self) -> None:
        """Pre-load the model so the first real request is instant."""
        await self._ensure_loaded()
        logger.info("EmbeddingPipeline warmed up.")
