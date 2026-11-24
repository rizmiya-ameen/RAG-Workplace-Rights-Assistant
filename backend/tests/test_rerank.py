"""Cross-encoder re-ranking: ordering, k, scores (fake scorer; the real model is checked in
the retrieval eval)."""

from app.retrieval.rerank import rerank
from app.retrieval.search import Hit


def _hit(ref: str, content: str) -> Hit:
    return Hit(1, "MA000009", ref, f"Heading {ref}", content, False, score=0.5, distance=0.3)


HITS = [_hit("11.1", "casual loading"), _hit("28.2", "overtime"), _hit("13A.1", "disconnect")]


def test_rerank_orders_by_score_and_keeps_k():
    seen = {}

    def scorer(query, passages):
        seen["query"], seen["passages"] = query, passages
        return [0.1, 5.0, 2.0]

    out = rerank("overtime rate?", HITS, k=2, scorer=scorer)
    assert [h.clause_ref for h in out] == ["28.2", "13A.1"]
    assert [h.rerank_score for h in out] == [5.0, 2.0]
    assert out[0].score == 0.5 and out[0].distance == 0.3  # retriever scores are kept
    assert seen["query"] == "overtime rate?"
    assert seen["passages"][1] == "Heading 28.2\novertime"  # same text bge embeds


def test_ties_keep_the_retriever_order():
    out = rerank("q", HITS, k=3, scorer=lambda q, p: [1.0, 1.0, 1.0])
    assert [h.clause_ref for h in out] == ["11.1", "28.2", "13A.1"]


def test_no_hits():
    assert rerank("q", [], k=6, scorer=lambda q, p: []) == []
