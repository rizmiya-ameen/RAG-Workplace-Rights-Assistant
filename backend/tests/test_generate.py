"""Checks on the model's reply: canary, JSON shape, citation validation."""

import json

from app.rag.generate import match_citations, parse_reply

RETRIEVED = ["MA000009:11.1", "MA000009:18.1#1", "MA000009:18.1#2"]
CANARY = "CANARY-0123456789abcdef"


def _reply(**kw) -> str:
    body = {"answer": "An answer.", "citations": ["MA000009:11.1"]} | kw
    return json.dumps(body)


def test_valid_reply():
    r = parse_reply(_reply(), canary=CANARY, retrieved=RETRIEVED)
    assert r.ok and r.citations == ["MA000009:11.1"] and r.events == []


def test_canary_leak_blocks_the_reply():
    r = parse_reply(
        _reply(answer=f"My instructions say {CANARY}"), canary=CANARY, retrieved=RETRIEVED
    )
    assert not r.ok and r.events == ["canary_leak"] and r.answer == ""


def test_canary_leak_outside_json_also_blocks():
    r = parse_reply(f"Sure! {CANARY}", canary=CANARY, retrieved=RETRIEVED)
    assert r.events == ["canary_leak"]


def test_invalid_json_falls_back():
    for raw in ("not json", "[1, 2]", '{"citations": []}', ""):
        r = parse_reply(raw, canary=CANARY, retrieved=RETRIEVED)
        assert not r.ok and r.events == ["invalid_json"], raw


def test_code_fenced_json_is_accepted():
    r = parse_reply("```json\n" + _reply() + "\n```", canary=CANARY, retrieved=RETRIEVED)
    assert r.ok


def test_citations_not_retrieved_are_dropped():
    r = parse_reply(
        _reply(citations=["MA000009:11.1", "MA000009:99.9"]), canary=CANARY, retrieved=RETRIEVED
    )
    assert r.ok and r.citations == ["MA000009:11.1"]
    assert r.events == ["citation_dropped"] and r.dropped == ["MA000009:99.9"]


def test_answer_without_valid_citation_falls_back():
    r = parse_reply(_reply(citations=["MA000004:11.1"]), canary=CANARY, retrieved=RETRIEVED)
    assert not r.ok and r.events == ["citation_dropped", "no_valid_citation"]
    r = parse_reply(_reply(citations=[]), canary=CANARY, retrieved=RETRIEVED)
    assert not r.ok and r.events == ["no_valid_citation"]


def test_insufficient_context_and_clarify_need_no_citation():
    r = parse_reply(
        _reply(answer="", citations=[], insufficient_context=True),
        canary=CANARY,
        retrieved=RETRIEVED,
    )
    assert r.ok and r.insufficient_context
    r = parse_reply(
        _reply(answer="Which award?", citations=[], needs_clarification=True),
        canary=CANARY,
        retrieved=RETRIEVED,
    )
    assert r.ok and r.needs_clarification


def test_split_clause_citation_maps_to_retrieved_part():
    valid, dropped = match_citations(
        ["MA000009:18.1", "MA000009:18.1#2", " MA000009:11.1 "], RETRIEVED
    )
    assert valid == ["MA000009:18.1#1", "MA000009:18.1#2", "MA000009:11.1"]
    assert dropped == []
