"""Cross-encoder re-ranking (ms-marco-MiniLM-L-6-v2 via fastembed, ONNX), loaded lazily.

The bi-encoder (bge) embeds question and clause separately, so it finds the right
neighbourhood but often orders it badly. A cross-encoder reads question and clause together
and scores how well the clause answers the question; it is too slow for 1,163 chunks, so it
re-orders the retriever's candidates only (24 by default).

Scores are raw logits (higher = more relevant, roughly -11 to +11); the relevance gate in
app/pipeline.py compares the top score with a threshold tuned on the golden set.
"""

from collections.abc import Callable, Sequence
from dataclasses import replace
from functools import lru_cache
from typing import TYPE_CHECKING

from app.config import get_settings
from app.embeddings import passage_text

if TYPE_CHECKING:
    from app.retrieval.search import Hit


@lru_cache
def _model():
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    settings = get_settings()
    return TextCrossEncoder(settings.reranker_model, cache_dir=str(settings.model_cache_dir))


def score_pairs(query: str, passages: Sequence[str]) -> list[float]:
    if not passages:
        return []
    batch = get_settings().reranker_batch_size
    return [float(s) for s in _model().rerank(query, list(passages), batch_size=batch)]


def rerank(
    query: str,
    hits: Sequence["Hit"],
    *,
    k: int,
    scorer: Callable[[str, Sequence[str]], list[float]] = score_pairs,
) -> list["Hit"]:
    """Score every hit against the query and return the best k, highest first. Each hit keeps
    its retriever score; `rerank_score` is set. Ties keep the retriever's order."""
    scores = scorer(query, [passage_text(h.heading, h.content) for h in hits])
    order = sorted(range(len(hits)), key=lambda i: (-scores[i], i))[:k]
    return [replace(hits[i], rerank_score=scores[i]) for i in order]
