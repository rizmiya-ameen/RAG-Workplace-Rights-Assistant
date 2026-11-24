"""Minimal client for an OpenAI-compatible chat completions endpoint (Gemini, Azure OpenAI,
Ollama, ...). Plain httpx: one POST, temperature 0, JSON mode, retries on busy/rate-limit.

Switching provider = LLM_BASE_URL, LLM_API_KEY and LLM_MODEL in backend/.env.

Time limits (step 19): each attempt waits at most LLM_TIMEOUT_S, and the whole call, retries
and backoff included, at most LLM_DEADLINE_S. Without the deadline, 3 attempts of 60 s plus
backoff kept a /chat request open for over 3 minutes.
"""

import time
from dataclasses import dataclass

import httpx

from app.config import Settings, get_settings

# Busy (503), rate limit (429) and gateway errors are worth another try; 4xx otherwise is not.
RETRY_STATUS = {429, 500, 502, 503, 504}
BACKOFF_S = (1.0, 3.0, 6.0)
MAX_RETRY_AFTER_S = 10.0


class LLMError(RuntimeError):
    """The model could not produce a reply (after retries). Message never contains the key."""


@dataclass
class LLMReply:
    content: str
    finish_reason: str | None
    usage: dict
    latency_ms: float
    attempts: int


def _wait_s(response: httpx.Response | None, attempt: int) -> float:
    if response is not None:
        try:
            return min(float(response.headers["retry-after"]), MAX_RETRY_AFTER_S)
        except (KeyError, ValueError):
            pass
    return BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]


def chat(
    messages: list[dict],
    *,
    json_mode: bool = True,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
    sleep=time.sleep,
    clock=time.monotonic,
) -> LLMReply:
    s = settings or get_settings()
    payload: dict = {
        "model": s.llm_model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": s.llm_max_tokens,
    }
    if s.llm_reasoning_effort:
        payload["reasoning_effort"] = s.llm_reasoning_effort
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    url = s.llm_base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {s.llm_api_key}"}
    http = client or httpx.Client(timeout=s.llm_timeout_s)
    started = time.perf_counter()
    deadline = clock() + s.llm_deadline_s
    last_error = "no attempt made"
    try:
        for attempt in range(s.llm_max_retries + 1):
            response = None
            timeout = min(s.llm_timeout_s, deadline - clock())
            try:
                response = http.post(url, headers=headers, json=payload, timeout=timeout)
            except httpx.TimeoutException:
                last_error = f"timeout after {timeout:.0f} s"
            except httpx.TransportError as exc:
                last_error = f"connection error: {type(exc).__name__}"
            else:
                if response.status_code == 200:
                    body = response.json()
                    choice = body["choices"][0]
                    return LLMReply(
                        content=choice["message"].get("content") or "",
                        finish_reason=choice.get("finish_reason"),
                        usage=body.get("usage") or {},
                        latency_ms=1000 * (time.perf_counter() - started),
                        attempts=attempt + 1,
                    )
                last_error = f"HTTP {response.status_code}"
                if response.status_code not in RETRY_STATUS:
                    break
            if attempt < s.llm_max_retries:
                wait = _wait_s(response, attempt)
                if clock() + wait >= deadline - 1.0:  # no time left for a useful attempt
                    last_error += f" (deadline {s.llm_deadline_s:.0f} s)"
                    break
                sleep(wait)
    finally:
        if client is None:
            http.close()
    raise LLMError(f"LLM request failed: {last_error}")
