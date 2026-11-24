"""Rate limits for POST /chat (OWASP LLM10, unbounded consumption).

Two layers, both sliding windows kept in memory:
- per client (IP): RATE_LIMIT_PER_MINUTE and RATE_LIMIT_PER_DAY, so one user or script
  cannot use up the service;
- global: CHAT_DAILY_BUDGET requests a day across everyone. Each /chat costs at most one LLM
  call, and the Gemini free tier stopped us at ~480 requests a day; the budget keeps the
  demo from hitting that wall mid-conversation (and caps cost on a paid model).

In memory = per process. That fits the single small container planned for Azure (step 21);
several replicas would need a shared store (Redis) instead. A refused request gets HTTP 429
with Retry-After and costs nothing (no search, no LLM call, no database write).

Kept in-house instead of slowapi: about 60 lines, no extra dependency, and the clock can be
replaced in tests.
"""

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass

from app.config import get_settings

MINUTE = 60.0
DAY = 86_400.0


@dataclass(frozen=True)
class Refusal:
    scope: str  # "client_minute", "client_day" or "global_day"
    retry_after_s: int


class ChatLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()  # FastAPI runs sync endpoints in a thread pool
        self._clients: dict[str, deque[float]] = defaultdict(deque)
        self._global: deque[float] = deque()

    def reset(self) -> None:
        with self._lock:
            self._clients.clear()
            self._global.clear()

    def check(self, client: str) -> Refusal | None:
        """Count one request from `client`, or refuse it (a refused request is not counted)."""
        s = get_settings()
        if not s.rate_limit_enabled:
            return None
        now = self.clock()
        with self._lock:
            hits = self._clients[client]
            _drop_older(hits, now - DAY)
            _drop_older(self._global, now - DAY)
            recent = sum(1 for t in hits if t > now - MINUTE)
            if recent >= s.rate_limit_per_minute:
                return Refusal("client_minute", _retry(hits, now, MINUTE, s.rate_limit_per_minute))
            if len(hits) >= s.rate_limit_per_day:
                return Refusal("client_day", _retry(hits, now, DAY, s.rate_limit_per_day))
            if len(self._global) >= s.chat_daily_budget:
                return Refusal("global_day", _retry(self._global, now, DAY, s.chat_daily_budget))
            hits.append(now)
            self._global.append(now)
            if len(self._clients) > 10_000:  # bound memory: forget idle clients
                self._forget_idle(now)
            return None

    def _forget_idle(self, now: float) -> None:
        for key in [k for k, v in self._clients.items() if not v or v[-1] < now - MINUTE]:
            del self._clients[key]


def _drop_older(hits: deque[float], cutoff: float) -> None:
    while hits and hits[0] <= cutoff:
        hits.popleft()


def _retry(hits: deque[float], now: float, window: float, limit: int) -> int:
    """Seconds until the oldest request that counts against `limit` leaves the window."""
    inside = [t for t in hits if t > now - window]
    oldest = inside[-limit] if len(inside) >= limit else inside[0]
    return max(1, int(oldest + window - now + 0.999))


limiter = ChatLimiter()
