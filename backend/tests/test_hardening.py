"""Step 19: rate limits (429), the LLM timeout path through /chat, CORS, structured logs."""

import functools
import json
import logging

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main
from app.config import get_settings
from app.llm import chat
from app.logs import JsonFormatter, client_hash
from app.pipeline import answer_question
from app.ratelimit import DAY, ChatLimiter, limiter
from tests.test_llm import FakeClock
from tests.test_pipeline import FakeConn, _ConnCtx, _llm, hits  # noqa: F401 (fixture)

client = TestClient(main.app)
QUESTION = {"question": "What is the casual loading?", "award": "MA000009"}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def limits(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "rate_limit_enabled", True)
    monkeypatch.setattr(s, "rate_limit_per_minute", 3)
    monkeypatch.setattr(s, "rate_limit_per_day", 5)
    monkeypatch.setattr(s, "chat_daily_budget", 8)
    return s


# --- limiter ------------------------------------------------------------------------------


def test_per_minute_limit_then_window_slides(limits):
    clock = Clock()
    lim = ChatLimiter(clock)
    assert [lim.check("a") for _ in range(3)] == [None, None, None]
    refusal = lim.check("a")
    assert refusal.scope == "client_minute" and refusal.retry_after_s == 60
    assert lim.check("b") is None  # other clients are not affected
    clock.now += 30
    assert lim.check("a").retry_after_s == 30
    clock.now += 30.5
    assert lim.check("a") is None


def test_refused_requests_are_not_counted(limits):
    clock = Clock()
    lim = ChatLimiter(clock)
    for _ in range(3):
        lim.check("a")
    for _ in range(20):
        assert lim.check("a") is not None  # hammering does not extend the wait
    clock.now += 61
    assert lim.check("a") is None


def test_per_day_limit(limits):
    clock = Clock()
    lim = ChatLimiter(clock)
    for _ in range(5):
        assert lim.check("a") is None
        clock.now += 61
    refusal = lim.check("a")
    assert refusal.scope == "client_day"
    assert DAY - 5 * 61 - 1 <= refusal.retry_after_s <= DAY
    clock.now += DAY
    assert lim.check("a") is None


def test_global_daily_budget_across_clients(limits):
    clock = Clock()
    lim = ChatLimiter(clock)
    for i in range(8):
        assert lim.check(f"client-{i}") is None
    assert lim.check("new-client").scope == "global_day"


def test_disabled_limiter_never_refuses(limits, monkeypatch):
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", False)
    lim = ChatLimiter(Clock())
    assert all(lim.check("a") is None for _ in range(50))


# --- 429 from /chat -----------------------------------------------------------------------


@pytest.fixture
def fake_backend(monkeypatch, hits):
    calls = []

    def fake_answer(conn, question, *, award=None):
        calls.append(question)
        return answer_question(
            conn, question, award=award, search_fn=lambda *a, **k: hits, chat_fn=_llm()
        )

    monkeypatch.setattr(main, "connect", lambda: _ConnCtx())
    monkeypatch.setattr(main, "answer_question", fake_answer)
    return calls


def test_chat_returns_429_with_retry_after(limits, fake_backend):
    statuses = [client.post("/chat", json=QUESTION).status_code for _ in range(4)]
    assert statuses == [200, 200, 200, 429]
    res = client.post("/chat", json=QUESTION)
    assert res.status_code == 429
    assert 1 <= int(res.headers["Retry-After"]) <= 60
    assert "wait a moment" in res.json()["detail"]
    assert len(fake_backend) == 3  # refused requests never reach search or the LLM


def test_health_is_not_rate_limited(limits, fake_backend):
    for _ in range(3):
        client.post("/chat", json=QUESTION)
    assert client.post("/chat", json=QUESTION).status_code == 429
    assert client.get("/health").status_code == 200


def test_limiter_resets_between_tests():
    assert limiter.check("testclient") is None


# --- timeout path through /chat -----------------------------------------------------------


def test_llm_timeout_returns_503_within_the_deadline(monkeypatch, hits):
    """A model that never answers: every attempt times out, the call stops at the deadline,
    /chat answers 503 with a retry message (and the key never appears)."""
    clock = FakeClock()
    s = get_settings()
    monkeypatch.setattr(s, "llm_timeout_s", 30.0)
    monkeypatch.setattr(s, "llm_deadline_s", 45.0)
    hanging = clock.client([httpx.ReadTimeout("no reply")] * 3)
    timing_out = functools.partial(chat, client=hanging, sleep=clock.sleep, clock=clock)

    def answer(conn, question, *, award=None):
        return answer_question(
            conn, question, award=award, search_fn=lambda *a, **k: hits, chat_fn=timing_out
        )

    monkeypatch.setattr(main, "connect", lambda: _ConnCtx())
    monkeypatch.setattr(main, "answer_question", answer)
    res = client.post("/chat", json=QUESTION)
    assert res.status_code == 503
    assert "try again" in res.json()["detail"]
    assert clock.now <= 45.0
    assert len(clock.timeouts) == 2


# --- CORS ---------------------------------------------------------------------------------


def _cors_app(origins: str) -> TestClient:
    app = FastAPI()
    app.get("/health")(lambda: {"status": "ok"})
    main.setup_cors(app, origins)
    return TestClient(app)


def test_cors_allows_only_listed_origins():
    c = _cors_app("https://rights.example.org, https://other.example.org")
    ok = c.options(
        "/health",
        headers={"Origin": "https://rights.example.org", "Access-Control-Request-Method": "GET"},
    )
    assert ok.headers["access-control-allow-origin"] == "https://rights.example.org"
    assert "access-control-allow-credentials" not in ok.headers
    bad = c.get("/health", headers={"Origin": "https://evil.example.com"})
    assert "access-control-allow-origin" not in bad.headers


def test_no_cors_headers_by_default():
    c = _cors_app("")
    res = c.get("/health", headers={"Origin": "https://rights.example.org"})
    assert "access-control-allow-origin" not in res.headers


# --- structured logs ----------------------------------------------------------------------


def test_json_formatter_puts_extra_fields_at_top_level():
    record = logging.LogRecord("app.http", logging.INFO, "", 0, "request", None, None)
    record.status = 200
    record.request_id = "abc"
    line = json.loads(JsonFormatter().format(record))
    assert line["msg"] == "request" and line["status"] == 200 and line["request_id"] == "abc"
    assert line["level"] == "INFO" and line["ts"].endswith("Z")


def test_client_hash_is_stable_and_not_the_ip():
    h = client_hash("203.0.113.7")
    assert h == client_hash("203.0.113.7") and h != client_hash("203.0.113.8")
    assert "203.0.113" not in h and len(h) == 12


def test_logs_have_no_question_answer_or_raw_ip(fake_backend, caplog):
    question = "What is the casual loading? My email is jo@example.com"
    with caplog.at_level(logging.INFO):
        res = client.post("/chat", json={"question": question, "award": "MA000009"})
    assert res.status_code == 200
    lines = [JsonFormatter().format(r) for r in caplog.records]
    text = "\n".join(lines)
    assert "casual loading" not in text and "jo@example.com" not in text
    assert res.json()["answer"] not in text
    assert "testclient" not in text  # TestClient's client host
    access = [json.loads(x) for x in lines if json.loads(x)["msg"] == "request"]
    assert access[-1]["status"] == 200 and access[-1]["path"] == "/chat"
    assert access[-1]["request_id"] == res.headers["X-Request-ID"]
    chat_line = [json.loads(x) for x in lines if json.loads(x)["msg"] == "chat"][-1]
    assert chat_line["route"] == "rag" and chat_line["request_id"] == res.json()["request_id"]


def test_rate_limited_request_is_logged_without_ip(limits, fake_backend, caplog):
    with caplog.at_level(logging.INFO):
        for _ in range(4):
            client.post("/chat", json=QUESTION)
    refused = [r for r in caplog.records if r.getMessage() == "rate_limited"]
    assert len(refused) == 1 and refused[0].scope == "client_minute"
    assert refused[0].client_hash == client_hash("testclient")
