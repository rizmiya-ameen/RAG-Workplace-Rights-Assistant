"""bge-small-en-v1.5 embeddings via fastembed (ONNX Runtime), loaded lazily on first use.

ONNX instead of torch: ~200 MB RAM instead of ~800 MB, and identical vectors (checked
against sentence-transformers: cosine 1.0000 on award chunks).

bge uses an instruction prefix on *queries only*; passages are embedded as-is.
Vectors are L2-normalised, so cosine distance in pgvector works directly.
"""

from functools import lru_cache

import numpy as np

from app.config import get_settings

DIMS = 384
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


@lru_cache
def _model():
    from fastembed import TextEmbedding

    settings = get_settings()
    return TextEmbedding(settings.embedding_model, cache_dir=str(settings.model_cache_dir))


def passage_text(heading: str, content: str) -> str:
    """What gets embedded for a chunk: its clause path plus its text."""
    return f"{heading}\n{content}" if heading else content


def embed_passages(texts: list[str]) -> list[list[float]]:
    vectors = np.array(
        list(_model().embed(texts, batch_size=get_settings().embedding_batch_size))
    ).reshape(len(texts), DIMS)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors.tolist()


def embed_query(text: str) -> list[float]:
    return embed_passages([QUERY_PREFIX + text])[0]
