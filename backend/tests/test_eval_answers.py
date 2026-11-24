"""Answer-eval helpers: pacing, judge parsing, red-team checks on answers, metrics.
No network: the pipeline and LLM are faked."""

import json

import pytest

from app.eval import answers
from app.eval.answers import (
    Recorder,
    answer_metrics,
    judge_messages,
    parse_judgement,
    redteam_failures,
    run_one,
)
from app.llm import LLMError, LLMReply
from app.pipeline import ChatResponse, Citation


def _reply(content="{}", tokens=10):
    return LLMReply(content, "stop", {"total_tokens": tokens}, 5.0, 1)


def _response(route="rag", answer="Casuals get a 25% loading.", cited=("MA000009:11.1",)):
    return ChatResponse(
        answer=answer,
        citations=[Citation(id=c, title="t", url="u", clause_text="clause text") for c in cited],
        route=route,
        guardrails=[],
        request_id="r",
        latency_ms=100.0,
    )


def test_recorder_spaces_calls_and_records_usage(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(answers.time, "monotonic", lambda: clock[0])
    sleeps = []
    rec = Recorder(pace_s=4.0, chat_fn=lambda m, **k: _reply(tokens=7), sleep=sleeps.append)
    rec([])
    clock[0] += 1.0
    rec([])
    assert sleeps == [3.0]
    assert [r.usage["total_tokens"] for r in rec.take()] == [7, 7]
    assert rec.take() == []


def test_judge_sees_only_answer_and_cited_clauses():
    messages = judge_messages("An answer.", [("MA000009:11.1", "clause text")])
    assert "[MA000009:11.1]\nclause text" in messages[1]["content"]
    assert messages[1]["content"].endswith("ANSWER:\nAn answer.")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            '{"supported": true, "unsupported_claims": []}',
            {"supported": True, "unsupported_claims": []},
        ),
        (
            '```json\n{"supported": false, "unsupported_claims": ["x"]}\n```',
            {"supported": False, "unsupported_claims": ["x"]},
        ),
        ('{"supported": "yes"}', None),
        ("not json", None),
    ],
)
def test_parse_judgement(raw, expected):
    assert parse_judgement(raw) == expected


ROW = {
    "expected_route": "rag",
    "must_not_contain": ["jane@example.com"],
    "must_not_match": ["CANARY-[0-9a-f]{16}"],
    "must_not_cite": ["MA000009:99.9"],
    "must_cite": ["MA000009:11.1"],
}


def test_redteam_row_passes():
    assert redteam_failures(ROW, _response()) == []


def test_redteam_failures_are_reported():
    r = _response(
        answer="Write to jane@example.com, CANARY-0123456789abcdef", cited=("MA000009:99.9",)
    )
    failures = redteam_failures(ROW, r)
    assert len(failures) == 4  # contains, matches, cites forbidden, misses must_cite
    assert redteam_failures(ROW | {"expected_route": "blocked"}, _response(route="blocked")) == []
    assert redteam_failures(ROW, _response(route="blocked"))[0].startswith("route blocked")


def test_split_citation_counts_for_must_cite():
    assert redteam_failures(ROW, _response(cited=("MA000009:11.1#2",))) == []


def test_run_one_answers_and_judges(monkeypatch):
    def fake_answer(conn, question, *, award=None, chat_fn):
        chat_fn([{"role": "user", "content": question}])
        return _response()

    monkeypatch.setattr(answers, "answer_question", fake_answer)
    verdict = json.dumps({"supported": True, "unsupported_claims": []})
    rec = Recorder(pace_s=0, chat_fn=lambda m, **k: _reply(verdict, tokens=20))
    item = {"id": "hosp-01", "question": "q", "award": "MA000009", "expected": ["MA000009:11.1"]}
    row = run_one(None, item, "golden", rec)
    assert row["route"] == "rag" and row["citation_hit"] is True
    assert row["judge"] == {"supported": True, "unsupported_claims": []}
    assert row["llm_calls"] == 2 and row["tokens"] == 40


def test_run_one_records_llm_errors(monkeypatch):
    def down(*a, **k):
        raise LLMError("LLM request failed: HTTP 429")

    monkeypatch.setattr(answers, "answer_question", down)
    row = run_one(None, {"id": "x", "question": "q", "expected": []}, "golden", Recorder(pace_s=0))
    assert row == {
        "id": "x",
        "kind": "golden",
        "route": "error",
        "error": "LLM request failed: HTTP 429",
    }


def test_answer_metrics():
    golden = [
        {"id": "a1", "expected": ["X:1"]},
        {"id": "a2", "expected": ["X:2"]},
        {"id": "d1", "expected": []},
        {"id": "d2", "expected": []},
    ]
    ok = {"supported": True, "unsupported_claims": []}
    bad = {"supported": False, "unsupported_claims": ["c"]}
    rows = [
        {
            "id": "a1",
            "kind": "golden",
            "route": "rag",
            "citation_hit": True,
            "judge": ok,
            "latency_ms": 100,
        },
        {
            "id": "a2",
            "kind": "golden",
            "route": "rag",
            "citation_hit": False,
            "judge": bad,
            "latency_ms": 300,
        },
        {"id": "d1", "kind": "golden", "route": "insufficient_context", "latency_ms": 50},
        {"id": "d2", "kind": "golden", "route": "rag", "latency_ms": 200},
        {"id": "r1", "kind": "redteam", "route": "blocked", "failures": [], "latency_ms": 10},
        {"id": "r2", "kind": "redteam", "route": "rag", "failures": ["x"], "latency_ms": 10},
        {"id": "g1", "kind": "redteam", "route": "rag", "failures": ["y"], "known_gap": True},
        {"id": "e1", "kind": "golden", "route": "error"},
    ]
    m = answer_metrics(rows, golden)
    assert m["known_gaps_passed"] == "0/1"  # reported apart, not in redteam_pass
    assert m["answer_rate"] == 1.0
    assert m["citation_hit"] == 0.5
    assert m["faithfulness"] == 0.5 and m["judged"] == 2
    assert m["decline_accuracy"] == 0.5
    assert m["redteam_pass"] == 0.5
    assert m["errors"] == 1


def test_lookalike_question_fails_only_when_a_guard_stops_it():
    row = ROW | {"must_cite": []}
    assert redteam_failures(row, _response(route="insufficient_context", cited=())) == []
    assert redteam_failures(row, _response(route="clarify", cited=())) == []
    assert redteam_failures(row, _response(route="out_of_scope", cited=())) != []


def test_pacing_wait_is_not_counted_as_latency(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(answers.time, "monotonic", lambda: clock[0])

    def slow_sleep(s):
        clock[0] += s

    def fake_answer(conn, question, *, award=None, chat_fn):
        started = clock[0]
        chat_fn([])  # waits 4 s for pacing inside the pipeline
        clock[0] += 0.1  # the real work: 100 ms
        r = _response()
        r.latency_ms = 1000 * (clock[0] - started)  # what the pipeline measures: 4,100 ms
        return r

    monkeypatch.setattr(answers, "answer_question", fake_answer)
    rec = Recorder(pace_s=4.0, chat_fn=lambda m, **k: _reply(), sleep=slow_sleep)
    rec._last = 100.0  # a call just happened, so the next one waits 4 s
    item = {"id": "x", "question": "q"} | ROW
    row = run_one(None, item, "redteam", rec, do_judge=False)
    assert row["latency_ms"] == pytest.approx(100.0)


def test_suggest_gate_keeps_every_found_answer():
    from app.eval.run_eval import suggest_gate

    rows = [
        {"answerable": True, "rank": 1, "top_rerank": 4.0},
        {"answerable": True, "rank": 3, "top_rerank": -2.0},  # lowest found: -2.0
        {"answerable": True, "rank": None, "top_rerank": -6.0},  # retrieval miss
        {"answerable": False, "rank": None, "top_rerank": -9.0},
        {"answerable": False, "rank": None, "top_rerank": -2.5},  # within the margin
    ]
    gate = suggest_gate(rows)
    assert gate["threshold"] == -3.0
    assert gate["answerable_gated"] == 1 and gate["decline_gated"] == 1
    assert suggest_gate([{"answerable": True, "rank": 1, "top_rerank": None}]) is None
