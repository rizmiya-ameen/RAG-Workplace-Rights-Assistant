"""Clause retrieval over the latest version of each source document.

    python -m app.retrieval.search "Do casuals get paid more on Sundays?" --mode hybrid
    python -m app.retrieval.search "overtime after 2 hours" --award MA000009 --mode vector

Modes:
- vector: cosine distance on bge-small embeddings (HNSW index);
- hybrid: vector top N and full-text top N, merged with reciprocal rank fusion (RRF, k=60).
  The full-text query ORs the question's distinctive words (AND over a whole question rarely
  matches; words in more than MAX_DF of chunks are dropped), ranked with ts_rank_cd.

Only the latest effective_date per source_id is searched (older award versions are kept for
history). `award` filters to one source_id, e.g. "MA000009".
"""

import argparse
from collections.abc import Callable
from dataclasses import dataclass

import psycopg

from app.db.conn import connect
from app.embeddings import embed_query
from app.retrieval.rerank import rerank as rerank_hits

CANDIDATES = 24  # per retriever, before fusion (and later, before re-ranking)
RRF_K = 60
MODES = ("vector", "hybrid")

_LATEST = """
    latest AS (
        SELECT DISTINCT ON (source_id) id, source_id
        FROM documents
        WHERE %(award)s::text IS NULL OR source_id = %(award)s
        ORDER BY source_id, effective_date DESC
    )"""

_VECTOR_SQL = f"""
    WITH {_LATEST}
    SELECT c.id, l.source_id, c.clause_ref, c.heading, c.content, c.is_rate_table,
           c.embedding <=> %(vec)s::vector AS distance
    FROM chunks c JOIN latest l ON l.id = c.document_id
    WHERE c.embedding IS NOT NULL
    ORDER BY c.embedding <=> %(vec)s::vector
    LIMIT %(n)s"""

# OR of the question's stemmed words, minus words in more than MAX_DF of all chunks
# ("employe" is in ~76%): ts_rank has no IDF, so common words would swamp the ranking.
# Lexemes are already stemmed, so the tsquery is built with the 'simple' config.
_TEXT_SQL = f"""
    WITH {_LATEST},
    words AS (
        SELECT s.word
        FROM ts_stat('SELECT tsv FROM chunks') s
        WHERE s.word = ANY (tsvector_to_array(to_tsvector('english', %(text)s)))
          AND s.ndoc <= %(max_df)s * (SELECT count(*) FROM chunks)
    ),
    q AS (SELECT to_tsquery('simple', string_agg(quote_literal(word), ' | ')) AS query FROM words)
    SELECT c.id, l.source_id, c.clause_ref, c.heading, c.content, c.is_rate_table,
           ts_rank_cd(c.tsv, q.query) AS rank
    FROM chunks c JOIN latest l ON l.id = c.document_id, q
    WHERE q.query IS NOT NULL AND c.tsv @@ q.query
    ORDER BY rank DESC, c.id
    LIMIT %(n)s"""
MAX_DF = 0.2


@dataclass
class Hit:
    chunk_id: int
    source_id: str
    clause_ref: str
    heading: str
    content: str
    is_rate_table: bool
    score: float  # vector: cosine similarity; hybrid: RRF score
    distance: float | None = None  # cosine distance, if the vector retriever found it
    rerank_score: float | None = None  # cross-encoder logit, if re-ranked (step 13)

    @property
    def citation(self) -> str:
        return f"{self.source_id}:{self.clause_ref}"


def rrf(rankings: list[list[int]], k: int = RRF_K) -> dict[int, float]:
    """Reciprocal rank fusion: score(id) = sum over rankings of 1 / (k + rank), rank from 1."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, 1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return scores


def _vector(conn, vec: list[float], award: str | None, n: int) -> list[tuple]:
    # Exact ordering even when the award filter removes most HNSW candidates (pgvector 0.8).
    conn.execute("SET LOCAL hnsw.iterative_scan = strict_order")
    return conn.execute(_VECTOR_SQL, {"vec": str(vec), "award": award, "n": n}).fetchall()


def _text(conn, text: str, award: str | None, n: int) -> list[tuple]:
    params = {"text": text, "award": award, "n": n, "max_df": MAX_DF}
    return conn.execute(_TEXT_SQL, params).fetchall()


def search(
    conn: psycopg.Connection,
    query: str,
    *,
    mode: str = "hybrid",
    award: str | None = None,
    k: int = 6,
    candidates: int = CANDIDATES,
    rerank: bool = False,
    embed: Callable[[str], list[float]] = embed_query,
    rerank_fn: Callable[..., list[Hit]] = rerank_hits,
) -> list[Hit]:
    """Top k hits. With rerank=True the retriever's top `candidates` are re-ordered by the
    cross-encoder and the best k returned (each with rerank_score)."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    vec_rows = _vector(conn, embed(query), award, candidates)
    rows = {r[0]: r for r in vec_rows}
    distance = {r[0]: r[6] for r in vec_rows}

    keep = candidates if rerank else k
    if mode == "vector":
        ranked = [(r[0], 1.0 - r[6]) for r in vec_rows[:keep]]
    else:
        text_rows = _text(conn, query, award, candidates)
        rows.update({r[0]: r for r in text_rows if r[0] not in rows})
        fused = rrf([[r[0] for r in vec_rows], [r[0] for r in text_rows]])
        ranked = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[:keep]

    hits = [Hit(*rows[cid][:6], score=score, distance=distance.get(cid)) for cid, score in ranked]
    return rerank_fn(query, hits, k=k) if rerank else hits


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("query")
    ap.add_argument("--mode", choices=MODES, default="hybrid")
    ap.add_argument("--award", help="source_id filter, e.g. MA000009")
    ap.add_argument("-k", type=int, default=6)
    ap.add_argument("--rerank", action="store_true", help="cross-encoder over the candidates")
    ap.add_argument("--full", action="store_true", help="print whole chunks")
    args = ap.parse_args()

    with connect() as conn:
        hits = search(
            conn, args.query, mode=args.mode, award=args.award, k=args.k, rerank=args.rerank
        )
    for i, h in enumerate(hits, 1):
        dist = f"dist {h.distance:.3f}" if h.distance is not None else "dist  -   "
        rr = f"  rerank {h.rerank_score:6.2f}" if h.rerank_score is not None else ""
        print(f"{i}. {h.citation:16} score {h.score:.4f}  {dist}{rr}  [{h.heading[:60]}]")
        body = h.content if args.full else h.content.replace("\n", " ")[:160]
        print(f"   {body}")


if __name__ == "__main__":
    main()
