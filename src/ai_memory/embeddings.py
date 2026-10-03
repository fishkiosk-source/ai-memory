"""Pluggable embedders. Hash (offline) | sentence-transformers | ollama."""
from __future__ import annotations
import hashlib
import math


DIM_DEFAULT = 384  # all-MiniLM-L6-v2 native dim; vec tables sized to this


class HashEmbedder:
    """Char-trigram hash into L2-normalized vec. Weak semantics, offline fallback."""

    dim = DIM_DEFAULT

    def __init__(self, dim: int = DIM_DEFAULT):
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        t = f" {text.lower()} "
        for i in range(len(t) - 2):
            h = int(hashlib.md5(t[i:i + 3].encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]


class STEmbedder:
    """sentence-transformers all-MiniLM-L6-v2, L2-normalized, 384-dim."""

    dim = DIM_DEFAULT

    def __init__(self, model: str = "all-MiniLM-L6-v2"):
        from sentence_transformers import SentenceTransformer  # type: ignore

        self.model = SentenceTransformer(model)

    def embed(self, text: str) -> list[float]:
        v = self.model.encode(text, normalize_embeddings=True)
        return list(map(float, v))


class OllamaEmbedder:
    """Ollama local server (e.g. nomic-embed-text). Requires `ollama serve`."""

    def __init__(self, model: str = "nomic-embed-text",
                 host: str = "http://localhost:11434", dim: int = DIM_DEFAULT):
        import urllib.request, json  # stdlib only

        self.model, self.host, self.dim = model, host, dim
        self._req, self._json = urllib.request, json

    def embed(self, text: str) -> list[float]:
        req = self._req.Request(
            f"{self.host}/api/embeddings",
            data=self._json.dumps({"model": self.model, "prompt": text}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with self._req.urlopen(req, timeout=30) as r:
            out = self._json.loads(r.read().decode())
        v = out["embedding"]
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v[: self.dim]]


def get_embedder(provider: str = "hash", dim: int = DIM_DEFAULT):
    provider = (provider or "hash").lower()
    if provider in ("st", "sentence-transformers", "miniLM"):
        return STEmbedder()
    if provider in ("ollama", "nomic"):
        return OllamaEmbedder(dim=dim)
    return HashEmbedder(dim)


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))
