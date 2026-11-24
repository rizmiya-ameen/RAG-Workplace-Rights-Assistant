"""Question -> input guard -> retrieval -> context guard -> LLM -> output guard -> answer.

    python -m app.pipeline "Do casuals get paid more on Sundays?" --award MA000009

Routes: rag, clarify (the model asks for a missing detail), insufficient_context, fallback
(reply failed a check), and from the input guard: blocked (injection, evasion), refer
(personal advice or dispute) and out_of_scope. Step 14 adds the router with the pay_rate
route; step 13 the re-ranker and relevance gate.
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
from app.guardrails.context_guard import check_context
from app.guardrails.input_guard import check_input
from app.guardrails.output_guard import check_output
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

BLOCKED_TEXT = (
    "I can only answer questions about Australian workplace awards, and I can't change how I "
    "work. Please ask about pay, hours, rosters, leave, breaks or other conditions."
)
ROUTE_TEXT = {
    "injection": BLOCKED_TEXT,
    "evasion": (
        "I can't help with getting around award obligations. Employers can get free help to "
        "comply from the Fair Work Ombudsman (13 13 94, fairwork.gov.au)."
    ),
    "refer": (
        "That sounds like a question about your own situation, which needs personal advice. "
        "The Fair Work Ombudsman (13 13 94, fairwork.gov.au) can help for free. Strict time "
        "limits can apply to dismissal and some other claims, so contact them soon. I can "
        "explain what an award says in general if you ask about the rule itself."
    ),
    "out_of_scope": (
        "I can only help with Australian workplace awards: pay, hours, rosters, leave, breaks "
        "and similar conditions under the Hospitality, General Retail, Fast Food and Clerks "
        "awards."
    ),
}

Route = Literal[
    "rag", "clarify", "insufficient_context", "fallback", "blocked", "refer", "out_of_scope"
]


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

    # 1. Input guard: decides the route before any retrieval or LLM call.
    checked = check_input(question)
    if checked.redactions:
        events.append(("input", "pii_redacted", checked.redactions))
    if checked.route != "pass":
        events.append(("input", checked.rule or checked.route, {}))
        kind = (checked.rule or "").split(":", 1)[0]
        text = ROUTE_TEXT.get(kind, ROUTE_TEXT.get(checked.route, BLOCKED_TEXT))
        route: Route = checked.route  # type: ignore[assignment]
        return respond(text, route, [])

    # 2. Retrieval on the redacted question, then the context guard.
    hits = search_fn(conn, checked.text, mode=s.retrieval_mode, award=award, k=s.retrieval_k)
    docs = _documents(conn, {h.source_id for h in hits}) if hits else {}
    context = check_context(hits, {sid: url for sid, (_, url) in docs.items()})
    for citation, reason in context.dropped.items():
        events.append(("context", "source_dropped", {"citation": citation, "reason": reason}))
    if context.cleaned:
        events.append(("context", "source_cleaned", {"count": context.cleaned}))
    hits = context.hits
    if not hits:
        return respond(INSUFFICIENT_TEXT, "insufficient_context", [])

    # 3. Generation.
    canary = new_canary()
    titles = {sid: title for sid, (title, _) in docs.items()}
    messages, masked = build_messages(checked.text, hits, canary, titles)
    if masked:
        events.append(("context", "amounts_masked", {"count": masked}))
    reply = chat_fn(messages)

    # 4. Output guard: reply checks (canary, JSON, citations), then the answer text.
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

    answer, output_events = check_output(parsed.answer)
    events.extend(("output", rule, detail) for rule, detail in output_events)
    cited = [by_id[c] for c in parsed.citations]
    return respond(answer, "clarify" if parsed.needs_clarification else "rag", cited)


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
