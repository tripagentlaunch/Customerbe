"""Pinecone-backed vector store for Aanya's grounded corpus (RAG retrieval).

Per Amit's direction, Pinecone replaces Supabase pgvector as the vector DB.
Pinecone stores and searches vectors only — it doesn't generate them. Embeddings
are generated LOCALLY via sentence-transformers (BAAI/bge-base-en-v1.5) — no
external embedding API, no vendor account, no per-request network call or cost.
This module owns both: index lifecycle + upsert/query against Pinecone, and
text -> vector via the local model.

backend/scripts/embed_corpus.py (one-time ingestion) and
backend/app/routers/ai_router.py (per-request retrieval) both import this.
"""

import logging
import os

from pinecone import Pinecone, ServerlessSpec
from sentence_transformers import SentenceTransformer

_log = logging.getLogger("vector_store")

# BAAI/bge-base-en-v1.5: strong, well-regarded general-purpose retrieval model
# (consistently top-tier on the MTEB retrieval leaderboard for its size class),
# 109M params / 768-dim — small enough to run comfortably on a CPU-only laptop,
# and its asymmetric convention is simpler than e5's (only the QUERY side needs
# an instruction prefix; passages/documents get none — one less place to get
# the prefixing wrong between ingestion and query time).
#
# The dimension is fixed into the Pinecone index at creation time — if you
# change MODEL_NAME to something with a different output size, you MUST delete
# and recreate the Pinecone index (see ensure_index() below, which refuses to
# silently reuse a mismatched index).
MODEL_NAME = "BAAI/bge-base-en-v1.5"
EMBEDDING_DIMENSION = 768
# bge's documented convention: prefix ONLY the query side for retrieval; the
# document/passage side is embedded as-is.
_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "
DEFAULT_TOP_K = 6


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


class VectorStore:
    def __init__(self):
        pinecone_api_key = _env("PINECONE_API_KEY")
        if not pinecone_api_key:
            raise RuntimeError(
                "PINECONE_API_KEY is not set. Add it to backend/.env (see .env.example) "
                "before calling the concierge chat endpoint or the ingestion script."
            )

        self._index_name = _env("PINECONE_INDEX_NAME", "tripagent-assistant-corpus")
        # Optional fast-path: a stored index host skips Pinecone's describe_index
        # lookup on every cold start. Safe to leave unset — Index(name=...) works
        # too, it just does one extra control-plane call to resolve the host.
        self._index_host = _env("PINECONE_INDEX_HOST")
        self._cloud = _env("PINECONE_CLOUD", "aws")
        self._region = _env("PINECONE_REGION", "us-east-1")

        self._pc = Pinecone(api_key=pinecone_api_key)
        self._index = None
        # Lazy-loaded: the first embed_documents()/embed_query() call downloads
        # the model (a few hundred MB, once, cached under ~/.cache/huggingface)
        # and loads it into memory. Every call after that reuses the same
        # in-process model — no repeated download or reload.
        self._model = None

    def _get_model(self) -> SentenceTransformer:
        if self._model is None:
            _log.info("Loading local embedding model %s (first run downloads it)...", MODEL_NAME)
            self._model = SentenceTransformer(MODEL_NAME)
        return self._model

    def ensure_index(self) -> None:
        """Create the Pinecone index if it doesn't already exist. Safe to call
        repeatedly (e.g. at the top of every ingestion run).

        Raises clearly instead of silently reusing an index created under a
        different embedding model's dimension (e.g. a leftover 1024-dim index
        from an earlier Voyage-based setup) — a dimension mismatch would
        otherwise only surface as an opaque Pinecone error on the first upsert."""
        if self._pc.has_index(self._index_name):
            existing_dim = _field(self._pc.describe_index(self._index_name), "dimension")
            if existing_dim is not None and existing_dim != EMBEDDING_DIMENSION:
                raise RuntimeError(
                    f"Pinecone index '{self._index_name}' already exists with dimension "
                    f"{existing_dim}, but {MODEL_NAME} produces {EMBEDDING_DIMENSION}-dim "
                    f"vectors. Delete the old index (Pinecone console, or "
                    f"`pc.delete_index('{self._index_name}')`) and re-run "
                    f"scripts/embed_corpus.py to recreate it at the correct dimension."
                )
            return
        _log.info("Creating Pinecone index %s (dim=%s, %s/%s)",
                   self._index_name, EMBEDDING_DIMENSION, self._cloud, self._region)
        self._pc.create_index(
            name=self._index_name,
            dimension=EMBEDDING_DIMENSION,
            metric="cosine",
            spec=ServerlessSpec(cloud=self._cloud, region=self._region),
        )

    def index(self):
        if self._index is None:
            self._index = (
                self._pc.Index(host=self._index_host)
                if self._index_host
                else self._pc.Index(name=self._index_name)
            )
        return self._index

    def embed_documents(self, texts: list[str], show_progress_bar: bool = True) -> list[list[float]]:
        """Embed corpus chunks for upsert (ingestion-time). No instruction
        prefix — bge only prefixes the query side (see embed_query)."""
        vectors = self._get_model().encode(
            texts, batch_size=32, normalize_embeddings=True, show_progress_bar=show_progress_bar
        )
        return vectors.tolist()

    def embed_query(self, text: str) -> list[float]:
        """Embed a member's question for retrieval (request-time)."""
        vectors = self._get_model().encode(
            [_QUERY_INSTRUCTION + text], normalize_embeddings=True, show_progress_bar=False
        )
        return vectors[0].tolist()

    def upsert(self, vectors: list[dict], namespace: str | None = None, batch_size: int = 100) -> int:
        """vectors: [{"id": str, "values": [float, ...], "metadata": {...}}, ...]"""
        idx = self.index()
        upserted = 0
        for i in range(0, len(vectors), batch_size):
            batch = vectors[i:i + batch_size]
            idx.upsert(vectors=batch, namespace=namespace)
            upserted += len(batch)
        return upserted

    def query(self, query_text: str, top_k: int = DEFAULT_TOP_K,
              filter: dict | None = None, namespace: str | None = None) -> list[dict]:
        """Embeds query_text locally, queries Pinecone, returns
        [{"id", "score", "metadata"}, ...] ordered by relevance — standard RAG."""
        vector = self.embed_query(query_text)
        idx = self.index()
        result = idx.query(
            vector=vector, top_k=top_k, include_metadata=True, filter=filter, namespace=namespace
        )
        return [
            {
                "id": _field(match, "id"),
                "score": _field(match, "score"),
                "metadata": _field(match, "metadata") or {},
            }
            for match in result.matches
        ]


def _field(obj, key):
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


_store: VectorStore | None = None


def get_vector_store() -> VectorStore:
    global _store
    if _store is None:
        _store = VectorStore()
    return _store
