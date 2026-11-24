import json

import httpx
import pytest

from app.config import Settings
from app.llm import LLMError, chat

SECRET = "test-key-do-not-print"


def _settings(**kw) -> Settings:
    base = {
        "llm_base_url": "https://llm.example/v1/",
        "llm_api_key": SECRET,
        "llm_model": "m",
        "llm_max_retries": 2,
        "llm_reasoning_effort": "low",
    }
    return Settings(_env_file=None, **(base | kw))


def _ok(content='{"answer": "x"}'):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 7},
        },
    )


def _client(responses, seen=None):
    queue = list(responses)

    def handler(request: httpx.Request):
        if seen is not None:
            seen.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_sends_openai_compatible_request():
    seen = []
    reply = chat(
        [{"role": "user", "content": "hi"}], settings=_settings(), client=_client([_ok()], seen)
    )
    req = seen[0]
    body = json.loads(req.content)
    assert str(req.url) == "https://llm.example/v1/chat/completions"
    assert req.headers["authorization"] == f"Bearer {SECRET}"
    assert body["model"] == "m"
    assert body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    assert body["reasoning_effort"] == "low"
    assert reply.content == '{"answer": "x"}'
    assert reply.attempts == 1
    assert reply.usage == {"total_tokens": 7}


def test_reasoning_effort_omitted_when_empty():
    seen = []
    chat([], settings=_settings(llm_reasoning_effort=""), client=_client([_ok()], seen))
    assert "reasoning_effort" not in json.loads(seen[0].content)


def test_retries_busy_and_rate_limited_then_succeeds():
    sleeps = []
    responses = [httpx.Response(503), httpx.Response(429, headers={"retry-after": "2"}), _ok()]
    reply = chat([], settings=_settings(), client=_client(responses), sleep=sleeps.append)
    assert reply.attempts == 3
    assert sleeps == [1.0, 2.0]  # backoff, then the server's Retry-After


def test_retry_after_is_capped():
    sleeps = []
    responses = [httpx.Response(429, headers={"retry-after": "600"}), _ok()]
    chat([], settings=_settings(), client=_client(responses), sleep=sleeps.append)
    assert sleeps == [10.0]


def test_gives_up_after_max_retries_without_leaking_the_key():
    responses = [httpx.Response(503)] * 3
    with pytest.raises(LLMError) as err:
        chat([], settings=_settings(), client=_client(responses), sleep=lambda s: None)
    assert "503" in str(err.value)
    assert SECRET not in str(err.value)


def test_does_not_retry_client_errors():
    seen = []
    with pytest.raises(LLMError, match="400"):
        chat(
            [],
            settings=_settings(),
            client=_client([httpx.Response(400)], seen),
            sleep=lambda s: None,
        )
    assert len(seen) == 1


def test_timeout_is_retried():
    responses = [httpx.ReadTimeout("slow"), _ok()]
    reply = chat([], settings=_settings(), client=_client(responses), sleep=lambda s: None)
    assert reply.attempts == 2


# --- time limits (step 19) ----------------------------------------------------------------


class FakeClock:
    """Monotonic clock the test moves: each request "takes" `per_request` seconds."""

    def __init__(self, per_request: float = 0.0):
        self.now = 0.0
        self.per_request = per_request
        self.timeouts: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += s

    def client(self, responses):
        queue = list(responses)

        def handler(request: httpx.Request):
            timeout = request.extensions["timeout"]["read"]
            self.timeouts.append(timeout)
            item = queue.pop(0)
            if isinstance(item, httpx.TimeoutException):
                self.now += timeout  # a hung request lasts exactly its timeout
                raise item
            self.now += self.per_request
            return item

        return httpx.Client(transport=httpx.MockTransport(handler))


def test_timeout_path_gives_up_at_the_deadline():
    # Every attempt hangs until its timeout: 30 s, then only the 14 s left after backoff.
    clock = FakeClock()
    responses = [httpx.ReadTimeout("slow")] * 3
    with pytest.raises(LLMError, match=r"timeout after \d+ s \(deadline 45 s\)"):
        chat(
            [],
            settings=_settings(llm_timeout_s=30.0, llm_deadline_s=45.0),
            client=clock.client(responses),
            sleep=clock.sleep,
            clock=clock,
        )
    assert clock.timeouts == [30.0, pytest.approx(14.0)]
    assert clock.now <= 45.0 + 1e-9  # never waits past the deadline


def test_each_attempt_timeout_shrinks_to_the_time_left():
    clock = FakeClock(per_request=20.0)
    responses = [httpx.Response(503), _ok()]
    reply = chat(
        [],
        settings=_settings(llm_timeout_s=30.0, llm_deadline_s=45.0),
        client=clock.client(responses),
        sleep=clock.sleep,
        clock=clock,
    )
    assert reply.attempts == 2  # 20 s, 1 s backoff, then a 24 s timeout for attempt 2
    assert clock.timeouts == [30.0, pytest.approx(24.0)]


def test_no_retry_when_backoff_would_pass_the_deadline():
    clock = FakeClock(per_request=40.0)
    responses = [httpx.Response(429, headers={"retry-after": "8"})]
    with pytest.raises(LLMError, match="HTTP 429"):
        chat(
            [],
            settings=_settings(llm_timeout_s=60.0, llm_deadline_s=45.0),
            client=clock.client(responses),
            sleep=clock.sleep,
            clock=clock,
        )
    assert len(clock.timeouts) == 1


def test_defaults_bound_a_request():
    s = Settings(_env_file=None)
    assert s.llm_timeout_s <= s.llm_deadline_s <= 60
    assert s.llm_max_tokens <= 4096
