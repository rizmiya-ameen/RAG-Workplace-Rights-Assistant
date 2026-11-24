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
