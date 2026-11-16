"""Ingest tests. A fake embedder keeps them fast and offline; the real model is exercised by
running `python -m app.ingestion.ingest`. DB tests roll back and use source_id TEST-*.
"""

from pathlib import Path

import psycopg
import pytest

from app import embeddings
from app.db.conn import apply_schema, connect
from app.embeddings import DIMS, QUERY_PREFIX, passage_text
from app.ingestion.ingest import ingest_document
from app.ingestion.parse_award import parse_award_html

FIXTURE = Path(__file__).parent / "fixtures" / "MA000002_excerpt.html"


def fake_embed(texts):
    return [[(i % 7 + 1) / 10] * DIMS for i, _ in enumerate(texts)]


def test_passage_text_puts_heading_first():
    assert passage_text("13A. Employee right to disconnect", "13A.1 â€¦") == (
        "13A. Employee right to disconnect\n13A.1 â€¦"
    )
    assert passage_text("", "body") == "body"


def test_query_prefix_only_on_queries(monkeypatch):
    seen = []

    class FakeModel:
        def embed(self, texts, **kwargs):
            seen.extend(texts)
            return [[3.0] * DIMS for _ in texts]

    monkeypatch.setattr(embeddings, "_model", lambda: FakeModel())
    [passage] = embeddings.embed_passages(["a passage"])
    embeddings.embed_query("a question")
    assert seen == ["a passage", QUERY_PREFIX + "a question"]
    assert abs(sum(x * x for x in passage) - 1.0) < 1e-6  # normalised


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


def meta(sha="sha-1"):
    return {
        "source_id": "TEST-MA000002",
        "doc_type": "award",
        "title": "Test copy",
        "url": "https://example.invalid",
        "effective_date": "2000-01-01",
        "sha256": sha,
        "fetched_at": "2000-01-01T00:00:00+00:00",
    }


def count(conn, doc_id):
    return conn.execute("SELECT count(*) FROM chunks WHERE document_id = %s", (doc_id,)).fetchone()[0]


def test_ingest_stores_chunks_with_embeddings(conn, chunks):
    doc_id, status = ingest_document(conn, meta(), chunks, embed=fake_embed)
    assert status == "stored"
    assert count(conn, doc_id) == len(chunks)
    ref, heading, dims, tsv_hit = conn.execute(
        """SELECT clause_ref, heading, vector_dims(embedding),
                  tsv @@ websearch_to_tsquery('english', 'disconnect')
           FROM chunks WHERE document_id = %s AND clause_ref = '13A.1'""",
        (doc_id,),
    ).fetchone()
    assert (ref, heading, dims, tsv_hit) == ("13A.1", "13A. Employee right to disconnect", DIMS, True)


def test_unchanged_version_is_skipped(conn, chunks):
    calls = []

    def counting_embed(texts):
        calls.append(len(texts))
        return fake_embed(texts)

    doc_id, _ = ingest_document(conn, meta(), chunks, embed=counting_embed)
    again_id, status = ingest_document(conn, meta(), chunks, embed=counting_embed)
    assert (again_id, status) == (doc_id, "skipped")
    assert calls == [len(chunks)]


def test_changed_page_replaces_chunks(conn, chunks):
    doc_id, _ = ingest_document(conn, meta("sha-1"), chunks, embed=fake_embed)
    new_id, status = ingest_document(conn, meta("sha-2"), chunks[:3], embed=fake_embed)
    assert (new_id, status) == (doc_id, "stored")
    assert count(conn, doc_id) == 3
    stored_sha = conn.execute("SELECT sha256 FROM documents WHERE id = %s", (doc_id,)).fetchone()[0]
    assert stored_sha == "sha-2"


def test_rate_table_flag_is_stored(conn):
    hosp = parse_award_html((FIXTURE.parent / "MA000009_excerpt.html").read_bytes()).chunks
    doc_id, _ = ingest_document(conn, meta(), hosp, embed=fake_embed)
    flagged = conn.execute(
        "SELECT clause_ref FROM chunks WHERE document_id = %s AND is_rate_table", (doc_id,)
    ).fetchall()
    assert ("18.4",) in flagged


def test_wrong_vector_size_is_refused(conn, chunks):
    with pytest.raises(ValueError, match="wrong number or size"):
        ingest_document(conn, meta(), chunks, embed=lambda texts: [[0.1] * 3 for _ in texts])
