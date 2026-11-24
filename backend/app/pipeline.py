"""Question -> retrieval -> LLM -> checked answer with citations.

    python -m app.pipeline "Do casuals get paid more on Sundays?" --award MA000009

Build step 10 covers the rag path only. Later steps add the input guard and router (pay_rate,
clarify, refer, out_of_scope), the re-ranker with a relevance gate and the full output guard.
"""

import argparse
import time
import uuid
from collections.abc import Callable
from typing import Literal

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from app.config import get_settings
from app.db.conn import connect
from app.llm import chat
from app.rag.generate import parse_reply
from app.rag.prompts import build_messages, new_canary
from app.retrieval.search import Hit, search

DISCLAIMER = (
    "General information only, not legal advice. Check the cited clauses, and contact the "
    "Fair Work Ombudsman on 13 13 94 about your own situation."
)
INSUFFICIENT_TEXT = (
    "I couldn't find this in the award clauses I have, so I can't answer it reliably. "
    "The Fair Work Ombudsman (13 13 94, fairwork.gov.au) can help."
)
FALLBACK_TEXT = (
    "Sorry, I couldn't produce a reliable answer to that. Please rephrase the question, or "
    "contact the Fair Work Ombudsman on 13 13 94."
)

Route = Literal["rag", "clarify", "insufficient_context", "fallback"]


class Citation(BaseModel):
    id: str
    title: str
    url: str
    clause_text: str


class ChatResponse(BaseModel):
    answer: str
    citations: list[Citation]
    route: Route
    guardrails: list[str]
    request_id: str
    disclaimer: str = DISCLAIMER
    latency_ms: float


_DOCS_SQL = """
    SELECT DISTINCT ON (source_id) source_id, title, url
    FROM documents WHERE source_id = ANY(%s)
    ORDER BY source_id, effective_date DESC"""


def _documents(conn, source_ids: set[str]) -> dict[str, tuple[str, str]]:
    rows = conn.execute(_DOCS_SQL, (list(source_ids),)).fetchall()
    return {sid: (title, url) for sid, title, url in rows}


def log_events(conn, request_id: str, events: list[tuple[str, str, dict]]) -> None:
    """guardrail_events rows: (stage, rule, detail). Never store the question or answer.
    The caller commits (`with connect() as conn` commits on exit)."""
    for stage, rule, detail in events:
        conn.execute(
            "INSERT INTO guardrail_events (request_id, stage, rule, detail) VALUES (%s, %s, %s, %s)",
            (request_id, stage, rule, Jsonb(detail)),
        )


def answer_question(
    conn: psycopg.Connection,
    question: str,
    *,
    award: str | None = None,
    search_fn: Callable[..., list[Hit]] = search,
    chat_fn: Callable = chat,
) -> ChatResponse:
    """Raises app.llm.LLMError if the model can't be reached after retries."""
    s = get_settings()
    started = time.perf_counter()
    request_id = uuid.uuid4().hex
    hits = search_fn(conn, question, mode=s.retrieval_mode, award=award, k=s.retrieval_k)
    events: list[tuple[str, str, dict]] = []

    def respond(answer: str, route: Route, cited: list[Hit]) -> ChatResponse:
        docs = _documents(conn, {h.source_id for h in cited}) if cited else {}
        log_events(conn, request_id, events)
        return ChatResponse(
            answer=answer,
            citations=[
                Citation(
                    id=h.citation,
                    title=docs.get(h.source_id, ("", ""))[0],
                    url=docs.get(h.source_id, ("", ""))[1],
                    clause_text=h.content,
                )
                for h in cited
            ],
            route=route,
            guardrails=[rule for _, rule, _ in events],
            request_id=request_id,
            latency_ms=round(1000 * (time.perf_counter() - started), 1),
        )

    if not hits:
        return respond(INSUFFICIENT_TEXT, "insufficient_context", [])

    canary = new_canary()
    titles = {sid: t for sid, (t, _) in _documents(conn, {h.source_id for h in hits}).items()}
    messages, masked = build_messages(question, hits, canary, titles)
    if masked:
        events.append(("context", "amounts_masked", {"count": masked}))

    reply = chat_fn(messages)
    by_id = {h.citation: h for h in hits}
    parsed = parse_reply(reply.content, canary=canary, retrieved=list(by_id))
    for rule in parsed.events:
        detail = {"dropped": parsed.dropped} if rule == "citation_dropped" else {}
        if rule == "invalid_json":
            detail = {"finish_reason": reply.finish_reason}
        events.append(("output", rule, detail))

    if not parsed.ok:
        return respond(FALLBACK_TEXT, "fallback", [])
    if parsed.insufficient_context:
        return respond(INSUFFICIENT_TEXT, "insufficient_context", [])
    cited = [by_id[c] for c in parsed.citations]
    if parsed.needs_clarification:
        return respond(parsed.answer, "clarify", cited)
    return respond(parsed.answer, "rag", cited)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("question")
    ap.add_argument("--award", help="source_id filter, e.g. MA000009")
    args = ap.parse_args()
    with connect() as conn:
        r = answer_question(conn, args.question, award=args.award)
    print(f"[{r.route}] {r.latency_ms:.0f} ms  guardrails={r.guardrails}\n")
    print(r.answer, "\n")
    for c in r.citations:
        print(f"  - {c.id}  {c.title}")
    print(f"\n{r.disclaimer}")


if __name__ == "__main__":
    main()
