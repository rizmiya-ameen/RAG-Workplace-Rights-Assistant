"""Parse, embed and store saved source pages, one document version at a time.

    python -m app.ingestion.ingest --all                  # the four awards
    python -m app.ingestion.ingest --award MA000009 [--force]
    python -m app.ingestion.ingest --nes                  # Fair Work Act Part 2-2
    python -m app.ingestion.ingest --worksafe             # WorkSafe Victoria pages

A version is (source_id, effective_date). If that version is already stored with the same
sha256 its chunks are skipped; otherwise they are replaced in a single transaction. Pay rates
(app.ingestion.parse_rates) are reloaded every run. Older versions are kept (queries read only
the latest effective_date per source).
"""

import argparse
import time
from collections.abc import Callable

import psycopg

from app.db.conn import apply_schema, connect
from app.embeddings import DIMS, embed_passages, passage_text
from app.ingestion.parse_award import Chunk, ParseResult, latest_raw, parse_award_html, validate
from app.ingestion.parse_nes import parse_nes_html, validate_nes
from app.ingestion.parse_rates import load_rates, parse_rates_html, validate_rates

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
    """Store chunks (skipped if the page is unchanged) and always reload the pay rates,
    which are cheap: no embedding."""
    path, meta = latest_raw(award_id)
    html = path.read_bytes()
    result = parse_award_html(html)
    problems = validate(result)
    rates = parse_rates_html(html, award_id)
    problems += validate_rates(rates)
    if problems:
        raise ValueError(f"{award_id} failed validation, not ingesting: {problems[:5]}")
    started = time.perf_counter()
    doc_id, status = ingest_document(conn, meta, result.chunks, force=force)
    n_rates = load_rates(conn, doc_id, meta["effective_date"], rates.rates)
    conn.commit()
    secs = time.perf_counter() - started
    return (
        f"{award_id} {meta['effective_date']}: {status}, {len(result.chunks)} chunks, "
        f"{sum(c.is_rate_table for c in result.chunks)} rate tables, {n_rates} pay rates, "
        f"document_id {doc_id}, {secs:.0f}s"
    )


def ingest_parsed(
    conn: psycopg.Connection,
    source_id: str,
    parse: Callable[[bytes], ParseResult],
    check: Callable[[ParseResult], list[str]],
    force: bool = False,
) -> str:
    """NES and WorkSafe pages: chunks only (no pay rates)."""
    path, meta = latest_raw(source_id)
    result = parse(path.read_bytes())
    problems = check(result)
    if problems:
        raise ValueError(f"{source_id} failed validation, not ingesting: {problems[:5]}")
    started = time.perf_counter()
    doc_id, status = ingest_document(conn, meta, result.chunks, force=force)
    conn.commit()
    return (
        f"{source_id} {meta['effective_date']}: {status}, {len(result.chunks)} chunks, "
        f"document_id {doc_id}, {time.perf_counter() - started:.0f}s"
    )


def main() -> None:
    from app.ingestion.fetch import STARTING_AWARDS

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--award", action="append")
    group.add_argument("--all", action="store_true")
    group.add_argument("--nes", action="store_true")
    group.add_argument("--worksafe", action="store_true")
    ap.add_argument("--force", action="store_true", help="re-embed even if unchanged")
    args = ap.parse_args()

    with connect() as conn:
        apply_schema(conn)
        if args.nes:
            print(ingest_parsed(conn, "NES", parse_nes_html, validate_nes, args.force))
        elif args.worksafe:
            from app.ingestion.fetch import WORKSAFE_PAGES
            from app.ingestion.parse_worksafe import parse_worksafe_html, validate_worksafe

            for slug in WORKSAFE_PAGES:
                msg = ingest_parsed(
                    conn, f"WSV:{slug}", parse_worksafe_html, validate_worksafe, args.force
                )
                print(msg, flush=True)
        else:
            for award_id in STARTING_AWARDS if args.all else args.award:
                print(ingest_award(conn, award_id, force=args.force), flush=True)
        total = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
        n_rates = conn.execute("SELECT count(*) FROM pay_rates").fetchone()[0]
    print(f"chunks in database: {total}, pay rates: {n_rates}")


if __name__ == "__main__":
    main()
