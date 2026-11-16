"""Parse, embed and store saved award pages, one document version at a time.

    python -m app.ingestion.ingest --all
    python -m app.ingestion.ingest --award MA000009 [--force]

A version is (source_id, effective_date). If that version is already stored with the same
sha256 it is skipped; otherwise its chunks are replaced in a single transaction. Older
versions are kept (queries read only the latest effective_date per source).
"""

import argparse
import time
from collections.abc import Callable

import psycopg

from app.db.conn import apply_schema, connect
from app.embeddings import DIMS, embed_passages, passage_text
from app.ingestion.parse_award import Chunk, latest_raw, parse_award_html, validate

Embedder = Callable[[list[str]], list[list[float]]]


def ingest_document(
    conn: psycopg.Connection,
    meta: dict,
    chunks: list[Chunk],
    embed: Embedder = embed_passages,
    force: bool = False,
) -> tuple[int, str]:
    """Store one document version and its chunks. Returns (document_id, 'skipped'|'stored').

    Doesn't commit: the caller owns the transaction.
    """
    row = conn.execute(
        "SELECT id, sha256 FROM documents WHERE source_id = %s AND effective_date = %s",
        (meta["source_id"], meta["effective_date"]),
    ).fetchone()
    if row and row[1] == meta["sha256"] and not force:
        return row[0], "skipped"

    vectors = embed([passage_text(c.heading, c.content) for c in chunks])
    if len(vectors) != len(chunks) or any(len(v) != DIMS for v in vectors):
        raise ValueError("embedder returned the wrong number or size of vectors")

    doc_id = conn.execute(
        """INSERT INTO documents (source_id, doc_type, title, url, effective_date, sha256, fetched_at)
           VALUES (%(source_id)s, %(doc_type)s, %(title)s, %(url)s, %(effective_date)s,
                   %(sha256)s, %(fetched_at)s)
           ON CONFLICT (source_id, effective_date) DO UPDATE
             SET title = EXCLUDED.title, url = EXCLUDED.url, sha256 = EXCLUDED.sha256,
                 fetched_at = EXCLUDED.fetched_at, doc_type = EXCLUDED.doc_type
           RETURNING id""",
        meta,
    ).fetchone()[0]

    conn.execute("DELETE FROM chunks WHERE document_id = %s", (doc_id,))
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO chunks (document_id, clause_ref, heading, content, is_rate_table, embedding)
               VALUES (%s, %s, %s, %s, %s, %s::vector)""",
            [
                (doc_id, c.clause_ref, c.heading, c.content, c.is_rate_table, str(v))
                for c, v in zip(chunks, vectors, strict=True)
            ],
        )
    return doc_id, "stored"


def ingest_award(conn: psycopg.Connection, award_id: str, force: bool = False) -> str:
    path, meta = latest_raw(award_id)
    result = parse_award_html(path.read_bytes())
    problems = validate(result)
    if problems:
        raise ValueError(f"{award_id} failed validation, not ingesting: {problems[:5]}")
    started = time.perf_counter()
    doc_id, status = ingest_document(conn, meta, result.chunks, force=force)
    conn.commit()
    secs = time.perf_counter() - started
    return (
        f"{award_id} {meta['effective_date']}: {status}, {len(result.chunks)} chunks, "
        f"{sum(c.is_rate_table for c in result.chunks)} rate tables, document_id {doc_id}, "
        f"{secs:.0f}s"
    )


def main() -> None:
    from app.ingestion.fetch import STARTING_AWARDS

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--award", action="append")
    group.add_argument("--all", action="store_true")
    ap.add_argument("--force", action="store_true", help="re-embed even if unchanged")
    args = ap.parse_args()

    with connect() as conn:
        apply_schema(conn)
        for award_id in STARTING_AWARDS if args.all else args.award:
            print(ingest_award(conn, award_id, force=args.force), flush=True)
        total = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    print(f"chunks in database: {total}")


if __name__ == "__main__":
    main()
