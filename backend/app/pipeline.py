"""Question -> input guard -> retrieval -> context guard -> LLM -> output guard -> answer.

    python -m app.pipeline "Do casuals get paid more on Sundays?" --award MA000009

Routes: rag, pay_rate (amount from the pay tool), clarify (the router or the model asks for
a missing detail), insufficient_context, fallback (reply failed a check), and from the input
guard: blocked (injection, evasion), refer (personal advice or dispute) and out_of_scope.

After the input guard, app/router.py picks pay_rate / clarify / rag (step 14). rag:
retrieval with the re-ranker and relevance gate (step 13), context guard, LLM, output guard.
"""

import argparse
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from typing import Literal

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from app import observability as obs
from app.config import get_settings
from app.db.conn import connect
from app.guardrails.context_guard import check_context
from app.guardrails.input_guard import check_input
from app.guardrails.output_guard import check_output
from app.llm import chat
from app.rag.generate import parse_reply
from app.rag.pay_answer import phrase
from app.rag.prompts import build_messages, new_canary
from app.retrieval.search import Hit, clause_hits, search
from app.router import clarify_text, route_question
from app.tools import pay_rates

log = logging.getLogger("app.pipeline")

DISCLAIMER = (
    "General information only, not legal advice. Check the cited clauses, and contact the "
    "Fair Work Ombudsman on 13 13 94 about your own situation."
)
INSUFFICIENT_TEXT = (
    "I couldn't find this in the sources I have (four awards, the National Employment "
    "Standards and WorkSafe Victoria guidance), so I can't answer it reliably. "
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
    # The classifier also flags some honest phrasings ("Ignore my last question. ..."), so its
    # message asks for the question on its own instead of accusing the user.
    "injection:classifier": (
        "Your message reads like an instruction to the assistant, so I didn't act on it. If "
        "you have a question about pay, hours, leave or safety at work, please ask it on its "
        'own, for example: "What is the casual loading in hospitality?"'
    ),
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
        "I can only help with Australian workplace rights: pay, hours, rosters, leave, breaks "
        "and similar conditions under the Hospitality, General Retail, Fast Food and Clerks "
        "awards and the National Employment Standards, and workplace health and safety in "
        "Victoria."
    ),
}

NO_RATE_TEXT = (
    "I couldn't find that classification in the award's adult pay tables. Please check the "
    "level on your payslip or contract; the Fair Work Ombudsman's Pay Calculator "
    "(calculate.fairwork.gov.au) covers junior, apprentice and other rates."
)

Route = Literal[
    "rag",
    "pay_rate",
    "clarify",
    "insufficient_context",
    "fallback",
    "blocked",
    "refer",
    "out_of_scope",
]


class Citation(BaseModel):
    id: str
    title: str
    url: str
    clause_text: str
    heading: str = ""  # clause / section heading, shown above the text in the web UI


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
    """Raises app.llm.LLMError if the model can't be reached after retries. One Langfuse trace
    per call (app/observability.py; a no-op without keys), with the request_id as trace id."""
    request_id = uuid.uuid4().hex
    with obs.trace(request_id, metadata={"award": award}) as root:
        response = _answer(conn, question, award, search_fn, chat_fn, request_id, root)
        root.update(
            output={
                "route": response.route,
                "answer": response.answer,
                "citations": [c.id for c in response.citations],
                "guardrails": response.guardrails,
            },
            metadata={"latency_ms": response.latency_ms},
        )
    # Structured fields only (app/logs.py); never the question or the answer.
    log.info(
        "chat",
        extra={
            "request_id": request_id,
            "route": response.route,
            "latency_ms": round(response.latency_ms),
            "guardrails": response.guardrails,
            "citations": len(response.citations),
        },
    )
    return response


def _answer(conn, question, award, search_fn, chat_fn, request_id, root) -> ChatResponse:
    s = get_settings()
    started = time.perf_counter()
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
                    heading=h.heading or "",
                )
                for h in cited
            ],
            route=route,
            guardrails=[rule for _, rule, _ in events],
            request_id=request_id,
            latency_ms=round(1000 * (time.perf_counter() - started), 1),
        )

    # 1. Input guard: decides the route before any retrieval or LLM call. The trace only ever
    #    gets the guarded (normalised, PII-redacted) text.
    with obs.step("input_guard", "guardrail") as span:
        checked = check_input(question)
        span.update(
            output={
                "route": checked.route,
                "rule": checked.rule,
                "classifier_score": checked.score,
                "pii_redacted": checked.redactions,
            }
        )
    root.update(input={"question": checked.text, "award": award})
    if checked.redactions:
        events.append(("input", "pii_redacted", checked.redactions))
    if checked.route != "pass":
        detail = {"score": checked.score} if checked.score is not None else {}
        events.append(("input", checked.rule or checked.route, detail))
        kind = (checked.rule or "").split(":", 1)[0]
        text = ROUTE_TEXT.get(checked.rule or "") or ROUTE_TEXT.get(
            kind, ROUTE_TEXT.get(checked.route, BLOCKED_TEXT)
        )
        route: Route = checked.route  # type: ignore[assignment]
        return respond(text, route, [])

    # 2. Router: amount questions go to the pay tool, or get a clarifying question.
    with obs.step("router", input={"question": checked.text, "award": award}) as span:
        decision = route_question(checked.text, award)
        span.update(output=asdict(decision))
    if decision.route == "clarify":
        levels = None
        if decision.award:
            levels = pay_rates.short_levels(pay_rates.classifications(conn, decision.award))
        return respond(clarify_text(decision, levels), "clarify", [])
    if decision.route == "pay_rate":
        slots = {
            "award": decision.award,
            "level": decision.level,
            "employment_type": decision.employment_type,
            "days": list(decision.days),
            "casino": decision.casino,
        }
        with obs.step("pay_tool", "tool", input=slots) as span:
            rates = pay_rates.lookup(
                conn,
                decision.award,
                level=decision.level,
                employment_type=decision.employment_type,
                days=decision.days,
                casino=decision.casino,
            )
            span.update(output=[pay_rates.describe(r) for r in rates])
        if not rates:
            events.append(("output", "pay_no_rate", {"award": decision.award}))
            return respond(NO_RATE_TEXT, "insufficient_context", [])
        answer, pay_events = phrase(
            checked.text,
            decision.award,
            rates,
            canary=new_canary(),
            chat_fn=obs.traced_chat(chat_fn, "llm_phrase_pay"),
        )
        events.extend(("output", rule, detail) for rule, detail in pay_events)
        refs = list(dict.fromkeys(r.source_clause for r in rates))
        return respond(answer, "pay_rate", clause_hits(conn, decision.award, refs))

    # 3. Retrieval on the redacted question, then the context guard.
    query = {
        "query": checked.text,
        "mode": s.retrieval_mode,
        "award": award,
        "k": s.retrieval_k,
        "rerank": s.retrieval_rerank,
    }
    with obs.step("retrieval", "retriever", input=query) as span:
        hits = search_fn(
            conn,
            checked.text,
            mode=s.retrieval_mode,
            award=award,
            k=s.retrieval_k,
            rerank=s.retrieval_rerank,
        )
        span.update(
            output=[
                {
                    "id": h.citation,
                    "distance": None if h.distance is None else round(h.distance, 4),
                    "rerank_score": None if h.rerank_score is None else round(h.rerank_score, 3),
                }
                for h in hits
            ]
        )
    docs = _documents(conn, {h.source_id for h in hits}) if hits else {}
    with obs.step("context_guard", "guardrail") as span:
        context = check_context(hits, {sid: url for sid, (_, url) in docs.items()})
        span.update(
            output={
                "kept": [h.citation for h in context.hits],
                "dropped": context.dropped,
                "cleaned": context.cleaned,
            }
        )
    for citation, reason in context.dropped.items():
        events.append(("context", "source_dropped", {"citation": citation, "reason": reason}))
    if context.cleaned:
        events.append(("context", "source_cleaned", {"count": context.cleaned}))
    hits = context.hits
    if not hits:
        return respond(INSUFFICIENT_TEXT, "insufficient_context", [])

    # Relevance gate: if even the best clause scores low with the cross-encoder, the sources
    # don't cover the question; say so without calling the LLM (threshold tuned in step 13).
    top = hits[0].rerank_score
    gated = top is not None and top < s.relevance_threshold
    with obs.step("relevance_gate", "guardrail") as span:
        span.update(output={"top_score": top, "threshold": s.relevance_threshold, "gated": gated})
    if gated:
        events.append(("context", "relevance_gate", {"top_score": round(top, 2)}))
        return respond(INSUFFICIENT_TEXT, "insufficient_context", [])

    # 4. Generation.
    canary = new_canary()
    titles = {sid: title for sid, (title, _) in docs.items()}
    messages, masked = build_messages(checked.text, hits, canary, titles)
    if masked:
        events.append(("context", "amounts_masked", {"count": masked}))
    reply = obs.traced_chat(chat_fn)(messages)

    # 5. Output guard: reply checks (canary, JSON, citations), then the answer text.
    with obs.step("output_guard", "guardrail") as span:
        by_id = {h.citation: h for h in hits}
        parsed = parse_reply(reply.content, canary=canary, retrieved=list(by_id))
        answer, output_events = check_output(parsed.answer) if parsed.ok else ("", [])
        span.update(
            output={
                "ok": parsed.ok,
                "events": parsed.events + [rule for rule, _ in output_events],
                "citations": parsed.citations,
                "dropped": parsed.dropped,
            }
        )
    for rule in parsed.events:
        detail = {"dropped": parsed.dropped} if rule == "citation_dropped" else {}
        if rule == "invalid_json":
            detail = {"finish_reason": reply.finish_reason}
        events.append(("output", rule, detail))
    if not parsed.ok:
        return respond(FALLBACK_TEXT, "fallback", [])
    if parsed.insufficient_context:
        return respond(INSUFFICIENT_TEXT, "insufficient_context", [])

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
    obs.flush()
    print(f"[{r.route}] {r.latency_ms:.0f} ms  request_id={r.request_id}")
    print(f"guardrails={r.guardrails}\n")
    print(r.answer, "\n")
    for c in r.citations:
        print(f"  - {c.id}  {c.title}")
    print(f"\n{r.disclaimer}")


if __name__ == "__main__":
    main()
