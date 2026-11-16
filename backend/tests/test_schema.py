"""Schema tests against the Compose Postgres. Skipped if the database isn't running.

Each test runs inside a transaction that is rolled back, so no rows are left behind.
Rows use obvious placeholders (TEST-DOC), never real or invented award text.
"""

import psycopg
import pytest
from psycopg import errors

from app.db.conn import apply_schema, connect

DIMS = 384


def _vec(dims: int) -> str:
    return "[" + ",".join(["0.1"] * dims) + "]"


@pytest.fixture(scope="module")
def schema_ready():
    try:
        conn = connect(connect_timeout=3, autocommit=True)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres not reachable: {exc}")
    with conn:
        apply_schema(conn)
        apply_schema(conn)  # must be idempotent


@pytest.fixture
def conn(schema_ready):
    with connect() as c:
        yield c
        c.rollback()


@pytest.fixture
def doc_id(conn):
    return conn.execute(
        """INSERT INTO documents (source_id, doc_type, title, url, effective_date, sha256)
           VALUES ('TEST-DOC', 'award', 'Test document', 'https://example.invalid',
                   '2000-01-01', 'test') RETURNING id"""
    ).fetchone()[0]


def test_tables_exist(conn):
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert {"documents", "chunks", "pay_rates", "guardrail_events", "eval_runs"} <= names


def test_chunk_indexes_exist(conn):
    rows = conn.execute(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'chunks'"
    ).fetchall()
    defs = {name: d for name, d in rows}
    assert "hnsw" in defs["chunks_embedding_hnsw"]
    assert "vector_cosine_ops" in defs["chunks_embedding_hnsw"]
    assert "gin" in defs["chunks_tsv_gin"]


def test_embedding_is_vector_384(conn):
    typ = conn.execute(
        """SELECT format_type(atttypid, atttypmod) FROM pg_attribute
           WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"""
    ).fetchone()[0]
    assert typ == f"vector({DIMS})"


def test_chunk_insert_fills_tsv(conn, doc_id):
    conn.execute(
        """INSERT INTO chunks (document_id, clause_ref, heading, content, embedding)
           VALUES (%s, 'TEST-1', 'Placeholder heading', 'placeholder penalty text', %s)""",
        (doc_id, _vec(DIMS)),
    )
    hit = conn.execute(
        """SELECT count(*) FROM chunks
           WHERE document_id = %s AND tsv @@ websearch_to_tsquery('english', 'penalties')""",
        (doc_id,),
    ).fetchone()[0]
    assert hit == 1


def test_wrong_embedding_size_rejected(conn, doc_id):
    with pytest.raises(errors.DataException), conn.transaction():
        conn.execute(
            """INSERT INTO chunks (document_id, clause_ref, content, embedding)
               VALUES (%s, 'TEST-1', 'placeholder', %s)""",
            (doc_id, _vec(3)),
        )


def test_duplicate_clause_ref_rejected(conn, doc_id):
    sql = "INSERT INTO chunks (document_id, clause_ref, content) VALUES (%s, 'TEST-1', 'x')"
    conn.execute(sql, (doc_id,))
    with pytest.raises(errors.UniqueViolation), conn.transaction():
        conn.execute(sql, (doc_id,))


def test_duplicate_document_version_rejected(conn, doc_id):
    with pytest.raises(errors.UniqueViolation), conn.transaction():
        conn.execute(
            """INSERT INTO documents (source_id, doc_type, title, url, effective_date, sha256)
               VALUES ('TEST-DOC', 'award', 'Dup', 'https://example.invalid',
                       '2000-01-01', 'test')"""
        )


@pytest.mark.parametrize(
    "column, bad_value",
    [("employment_type", "contractor"), ("rate_type", "bonus")],
)
def test_pay_rates_enums_enforced(conn, doc_id, column, bad_value):
    values = {"employment_type": "casual", "rate_type": "hourly", column: bad_value}
    with pytest.raises(errors.CheckViolation), conn.transaction():
        conn.execute(
            """INSERT INTO pay_rates (document_id, classification, employment_type, rate_type,
                                      amount, unit, source_clause, effective_date)
               VALUES (%s, 'TEST-LEVEL', %s, %s, 0, 'per hour', 'TEST-1', '2000-01-01')""",
            (doc_id, values["employment_type"], values["rate_type"]),
        )


def test_guardrail_stage_enforced(conn):
    with pytest.raises(errors.CheckViolation), conn.transaction():
        conn.execute(
            "INSERT INTO guardrail_events (request_id, stage, rule) VALUES ('t', 'nope', 'r')"
        )


def test_deleting_document_cascades(conn, doc_id):
    conn.execute(
        "INSERT INTO chunks (document_id, clause_ref, content) VALUES (%s, 'TEST-1', 'x')",
        (doc_id,),
    )
    conn.execute("DELETE FROM documents WHERE id = %s", (doc_id,))
    left = conn.execute("SELECT count(*) FROM chunks WHERE document_id = %s", (doc_id,))
    assert left.fetchone()[0] == 0
