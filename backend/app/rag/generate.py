"""Check the model's JSON reply before anything reaches the user.

parse_reply() returns a ParsedReply; `events` lists the checks that fired:
- canary_leak: the per-request canary appears in the reply (prompt leaked) -> blocked;
- invalid_json: not a JSON object with the expected fields -> fallback;
- citation_dropped: an id that was not among the retrieved sources -> removed;
- no_valid_citation: an answer without a single valid citation -> fallback.
"""

import json
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


class LLMAnswer(BaseModel):
    answer: str
    citations: list[str] = []
    insufficient_context: bool = False
    needs_clarification: bool = False


@dataclass
class ParsedReply:
    ok: bool  # False -> show the fallback text, not the model's answer
    answer: str = ""
    citations: list[str] = field(default_factory=list)  # valid retrieved ids, in order
    insufficient_context: bool = False
    needs_clarification: bool = False
    events: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


def _base(citation: str) -> str:
    return citation.split("#", 1)[0]


_SUBPARAGRAPH = re.compile(r"(?:\s*\([a-zA-Z0-9]{1,4}\))+$")


def match_citations(cited: list[str], retrieved: list[str]) -> tuple[list[str], list[str]]:
    """Map cited ids onto retrieved ids. An exact id ("MA000009:18.1#2") is that part; a whole
    clause ("MA000009:18.1") is every retrieved part of it ("#1", "#2"), so the user and the
    judge see all the text the answer drew on. A sub-paragraph the model adds
    ("MA000009:28.2(a)", "MA000003:18.3(b)(i)") cites the clause. The step 12 eval found
    both being cut short. Returns (valid, dropped)."""
    valid, dropped = [], []
    retrieved_set = set(retrieved)
    for raw in cited:
        cid = _SUBPARAGRAPH.sub("", str(raw).strip())
        matches = [cid] if cid in retrieved_set else [r for r in retrieved if _base(r) == cid]
        if not matches:
            dropped.append(cid)
        valid += [m for m in matches if m not in valid]
    return valid, dropped


def parse_reply(raw: str, *, canary: str, retrieved: list[str]) -> ParsedReply:
    if canary and canary in raw:
        return ParsedReply(ok=False, events=["canary_leak"])

    text = raw.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        data = LLMAnswer.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError):
        return ParsedReply(ok=False, events=["invalid_json"])

    valid, dropped = match_citations(data.citations, retrieved)
    events = ["citation_dropped"] if dropped else []
    reply = ParsedReply(
        ok=True,
        answer=data.answer.strip(),
        citations=valid,
        insufficient_context=data.insufficient_context,
        needs_clarification=data.needs_clarification,
        events=events,
        dropped=dropped,
    )
    if not reply.answer and not reply.insufficient_context:
        reply.ok = False
        reply.events.append("invalid_json")
    elif not (reply.insufficient_context or reply.needs_clarification) and not valid:
        reply.ok = False
        reply.events.append("no_valid_citation")
    return reply
