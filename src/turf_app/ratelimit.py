"""Per-client-IP rate limiting, in memory.

Each uvicorn worker keeps its own counts, so with N workers a client can get up
to N times the limit in the worst case. That's acceptable for keeping one
visitor from hogging a small server; it isn't billing-grade metering, and a
shared store (Redis) would be the next step if it ever needed to be exact.

The client IP comes from the header named by TURF_CLIENT_IP_HEADER when set
(production: ``X-Client-IP``, written by Caddy from Cloudflare's
CF-Connecting-IP, overwriting anything the visitor sent). X-Forwarded-For is
deliberately not used: Cloudflare keeps a visitor-supplied value in it, so
trusting it would let anyone pick a fresh IP per request.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    """``limit`` requests per ``window_s`` seconds for paths starting with ``prefix``."""

    prefix: str
    limit: int
    window_s: float
    method: str | None = None


# Most specific first; the first matching rule applies.
DEFAULT_RULES: tuple[Rule, ...] = (
    Rule("/api/markets", limit=60, window_s=60, method="POST"),
    Rule("/mcp", limit=120, window_s=60),
    Rule("/api/", limit=300, window_s=60),
)


class RateLimiter:
    def __init__(
        self,
        rules: Sequence[Rule] = DEFAULT_RULES,
        clock: Callable[[], float] = time.monotonic,
        max_tracked: int = 50_000,
    ) -> None:
        self._rules = tuple(rules)
        self._clock = clock
        self._max_tracked = max_tracked
        self._hits: dict[tuple[str, int], deque[float]] = {}

    def _rule_for(self, method: str, path: str) -> tuple[int, Rule] | None:
        for i, rule in enumerate(self._rules):
            if path.startswith(rule.prefix) and rule.method in (None, method):
                return i, rule
        return None

    def check(self, client: str, method: str, path: str) -> int | None:
        """Record a request. Returns seconds to wait if it's over the limit, else None."""
        match = self._rule_for(method, path)
        if match is None:
            return None
        index, rule = match
        now = self._clock()
        key = (client, index)
        hits = self._hits.get(key)
        if hits is None:
            if len(self._hits) >= self._max_tracked:
                # Bounded memory: under a flood of distinct IPs, start over
                # rather than grow without limit.
                self._hits.clear()
            hits = self._hits[key] = deque()
        while hits and hits[0] <= now - rule.window_s:
            hits.popleft()
        if len(hits) >= rule.limit:
            return max(1, math.ceil(hits[0] + rule.window_s - now))
        hits.append(now)
        return None
