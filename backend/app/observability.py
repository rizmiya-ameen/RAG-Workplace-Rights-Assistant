"""Tracing (Langfuse) and guardrail statistics (step 17).

    python -m app.observability check              # are the Langfuse keys valid?
    python -m app.observability stats --days 7     # guardrail decisions from guardrail_events

One id everywhere: the pipeline's `request_id` (32 hex characters) is the Langfuse trace id,
the `request_id` in the /chat response and the `request_id` column of guardrail_events, so
a user's report, the database row and the trace all join on it.

Each /chat request is one trace named "chat" with nested observations: input_guard
(guardrail), router, pay_tool (tool) or retrieval (retriever) -> context_guard ->
relevance_gate (guardrails), llm (generation: model, tokens, latency) and output_guard
(guardrail).

Privacy: the trace gets the guarded question (normalised, PII replaced by [EMAIL] etc.),
never the raw text, and `mask()` runs on everything sent as a second line: PII patterns and
the per-request canary are replaced. Without LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY in
backend/.env every call here is a no-op (tests, CI, offline use).
"""

import argparse
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from app.config import get_settings
from app.guardrails.input_guard import redact_pii

_CANARY = re.compile(r"CANARY-[0-9a-f]{16}")


def mask(*, data: Any, **_: Any) -> Any:
    """Langfuse mask hook: PII and canaries out of every string, at any depth."""
    if isinstance(data, str):
        return _CANARY.sub("[CANARY]", redact_pii(data)[0])
    if isinstance(data, dict):
        return {k: mask(data=v) for k, v in data.items()}
    if isinstance(data, list | tuple):
        return [mask(data=v) for v in data]
    return data


class _Noop:
    """Stands in for an observation when tracing is off."""

    def update(self, **_: Any) -> "_Noop":
        return self


NOOP = _Noop()


@lru_cache
def client():
    """The Langfuse client, or None when no keys are configured."""
    s = get_settings()
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        return None
    from langfuse import Langfuse

    return Langfuse(
        public_key=s.langfuse_public_key,
        secret_key=s.langfuse_secret_key,
        base_url=s.langfuse_host,
        environment=s.app_env,
        mask=mask,
    )


@contextmanager
def trace(request_id: str, *, name: str = "chat", metadata: dict | None = None) -> Iterator:
    """Root observation of one request; its trace id is `request_id`."""
    c = client()
    if c is None:
        yield NOOP
        return
    from langfuse import propagate_attributes

    with (
        propagate_attributes(trace_name=name, tags=["chat"]),
        c.start_as_current_observation(
            trace_context={"trace_id": request_id}, name=name, as_type="chain", metadata=metadata
        ) as root,
    ):
        yield root


@contextmanager
def step(name: str, as_type: str = "span", **fields: Any) -> Iterator:
    """A nested observation inside the current trace (input, output, metadata, model, ...)."""
    c = client()
    if c is None:
        yield NOOP
        return
    with c.start_as_current_observation(name=name, as_type=as_type, **fields) as obs:
        yield obs


def traced_chat(chat_fn, name: str = "llm"):
    """Wrap an LLM call in a generation observation: messages in, reply out, token usage."""

    def call(messages: list[dict]):
        s = get_settings()
        with step(name, "generation", model=s.llm_model, input=messages) as gen:
            reply = chat_fn(messages)
            usage = reply.usage or {}
            gen.update(
                output=reply.content,
                usage_details={
                    "input": int(usage.get("prompt_tokens", 0)),
                    "output": int(usage.get("completion_tokens", 0)),
                },
                metadata={
                    "finish_reason": reply.finish_reason,
                    "latency_ms": reply.latency_ms,
                    "attempts": reply.attempts,
                },
            )
            return reply

    return call


def flush() -> None:
    c = client()
    if c is not None:
        c.flush()


# --- guardrail statistics -----------------------------------------------------------------

_STATS_SQL = """
    SELECT stage, rule, count(*) AS events, count(DISTINCT request_id) AS requests
    FROM guardrail_events
    WHERE created_at >= now() - make_interval(days => %(days)s)
    GROUP BY stage, rule
    ORDER BY events DESC, stage, rule"""


def guardrail_stats(conn, days: int = 7) -> list[tuple[str, str, int, int]]:
    """(stage, rule, events, distinct requests) over the last `days` days."""
    return conn.execute(_STATS_SQL, {"days": days}).fetchall()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="check the Langfuse keys")
    st = sub.add_parser("stats", help="guardrail decisions per rule")
    st.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    if args.cmd == "check":
        c = client()
        if c is None:
            print("tracing off: set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY in backend/.env")
        else:
            print("Langfuse keys OK" if c.auth_check() else "Langfuse rejected the keys")
        return

    from app.db.conn import connect

    with connect() as conn:
        rows = guardrail_stats(conn, args.days)
    print(f"guardrail events, last {args.days} days")
    print(f"{'stage':8} {'rule':40} {'events':>7} {'requests':>9}")
    for stage, rule, events, requests in rows:
        print(f"{stage:8} {rule:40} {events:7d} {requests:9d}")
    if not rows:
        print("(none)")


if __name__ == "__main__":
    main()
