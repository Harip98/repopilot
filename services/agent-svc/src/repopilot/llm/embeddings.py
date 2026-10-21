"""Text embeddings through any OpenAI-compatible endpoint (Ollama's /v1 serves
nomic-embed-text), plus a deterministic fake for tests."""

import hashlib
import math
from typing import Protocol

import httpx
from openai import AsyncOpenAI

DIMENSIONS = 768


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAICompatEmbedder:
    def __init__(self, base_url: str, api_key: str, model: str, *,
                 http_client: httpx.AsyncClient | None = None):
        self.model = model
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key or "unused",
                                   http_client=http_client, max_retries=2)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), 32):
            resp = await self._client.embeddings.create(model=self.model, input=texts[start:start + 32])
            out += [item.embedding for item in sorted(resp.data, key=lambda d: d.index)]
        return out


class FakeEmbedder:
    """Bag-of-words hashing: texts sharing words get similar vectors. Deterministic."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vec = [0.0] * DIMENSIONS
            for word in text.lower().split():
                slot = int(hashlib.sha256(word.encode()).hexdigest(), 16) % DIMENSIONS
                vec[slot] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vectors.append([v / norm for v in vec])
        return vectors
