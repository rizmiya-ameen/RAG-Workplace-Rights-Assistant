"""Retrieval tests. DB tests store fixture chunks under TEST-* source ids with one-hot
embeddings (chunk i points along axis i), so vector ranking is predictable, and roll back."""

from dataclasses import replace
from pathlib import Path

import psycopg
import pytest

from app.db.conn import apply_schema, connect
from app.embeddings import DIMS
from app.ingestion.ingest import ingest_document
from app.ingestion.parse_award import parse_award_html
from app.retrieval.search import CANDIDATES, RRF_K, rrf, search

FIXTURE = Path(__file__).parent / "fixtures" / "MA000002_excerpt.html"
AWARD = "TEST-SEARCH"


def one_hot(i: int) -> list[float]:
    v = [0.0] * DIMS
    v[i % DIMS] = 1.0
    return v


def one_hot_embed(texts):
    return [one_hot(i) for i in range(len(texts))]


def axis0(query: str) -> list[float]:
    """Query embedder that always points at the first chunk."""
    return one_hot(0)


def test_rrf_sums_reciprocal_ranks():
    scores = rrf([[10, 20, 30], [30, 10]])
    assert scores[10] == pytest.approx(1 / (RRF_K + 1) + 1 / (RRF_K + 2))
    assert scores[20] == pytest.approx(1 / (RRF_K + 2))
    assert scores[30] == pytest.approx(1 / (RRF_K + 3) + 1 / (RRF_K + 1))
    assert max(scores, key=scores.get) == 10  # ranked well by both lists


def test_rrf_handles_empty_rankings():
    assert rrf([[], [5]]) == {5: 1 / (RRF_K + 1)}


@pytest.fixture(scope="module")
def schema_ready():
    try:
        conn = connect(connect_timeout=10, autocommit=True)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres not reachable: {exc}")
    with conn:
        apply_schema(conn)


@pytest.fixture
def conn(schema_ready):
    with connect() as c:
        yield c
        c.rollback()


@pytest.fixture
def chunks():
    return parse_award_html(FIXTURE.read_bytes()).chunks


def meta(date: str, sha: str) -> dict:
    return {
        "source_id": AWARD,
        "doc_type": "award",
        "title": "Test copy",
        "url": "https://example.invalid",
        "effective_date": date,
        "sha256": sha,
        "fetched_at": "2000-01-01T00:00:00+00:00",
    }


@pytest.fixture
def doc(conn, chunks):
    """An old and a new version of the same source; returns the new version's chunk ids."""
    ingest_document(conn, meta("1999-07-01", "old"), chunks, embed=one_hot_embed)
    new_id, _ = ingest_document(conn, meta("2000-07-01", "new"), chunks, embed=one_hot_embed)
    rows = conn.execute(
        "SELECT clause_ref, id FROM chunks WHERE document_id = %s", (new_id,)
    ).fetchall()
    return dict(rows)


def test_vector_search_ranks_nearest_chunk_first(conn, chunks, doc):
    target = 5
    hits = search(conn, "q", mode="vector", award=AWARD, embed=lambda _: one_hot(target))
    assert hits[0].clause_ref == chunks[target].clause_ref
    assert hits[0].citation == f"{AWARD}:{chunks[target].clause_ref}"
    assert hits[0].distance == pytest.approx(0.0, abs=1e-6)
    assert hits[0].score == pytest.approx(1.0, abs=1e-6)
    assert len(hits) == 6


def test_only_latest_version_and_chosen_award_are_searched(conn, doc):
    for mode in ("vector", "hybrid"):
        hits = search(conn, "right to disconnect", mode=mode, award=AWARD, k=20, embed=axis0)
        assert hits and all(h.source_id == AWARD for h in hits)
        assert {h.chunk_id for h in hits} <= set(doc.values())  # none from the 1999 version


def test_hybrid_adds_full_text_matches(conn, doc):
    # The query vector points at chunk 0, so only full-text search can find clause 13A.
    vector = search(conn, "right to disconnect", mode="vector", award=AWARD, embed=axis0)
    hybrid = search(conn, "right to disconnect", mode="hybrid", award=AWARD, embed=axis0)
    assert not any(h.clause_ref.startswith("13A") for h in vector)
    assert any(h.clause_ref.startswith("13A") for h in hybrid)


def test_unknown_mode_is_refused(conn):
    with pytest.raises(ValueError, match="mode must be one of"):
        search(conn, "q", mode="bm25", embed=axis0)


def test_rerank_reorders_the_candidate_pool(conn, doc):
    # The vector query points at chunk 0; the fake re-ranker prefers clause 13A, which is
    # outside the vector top 6 but inside the 24 candidates.
    seen = {}

    def fake_rerank(query, hits, *, k):
        seen["n"] = len(hits)
        ordered = sorted(hits, key=lambda h: not h.clause_ref.startswith("13A"))
        return [replace(h, rerank_score=1.0) for h in ordered[:k]]

    plain = search(conn, "q", mode="vector", award=AWARD, embed=axis0)
    hits = search(
        conn, "q", mode="vector", award=AWARD, embed=axis0, rerank=True, rerank_fn=fake_rerank
    )
    assert not any(h.clause_ref.startswith("13A") for h in plain)
    assert seen["n"] == min(CANDIDATES, len(doc))
    assert hits[0].clause_ref.startswith("13A") and len(hits) == 6
    assert all(h.rerank_score == 1.0 for h in hits)
