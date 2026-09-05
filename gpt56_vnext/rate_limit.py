from __future__ import annotations

import asyncio
from email.utils import parsedate_to_datetime
import time


class RateLimitGate:
    """One gate per upstream connection, shared by all candidate models."""

    def __init__(self, *, max_in_flight: int | None = None):
        self._condition = asyncio.Condition()
        self._remaining = None
        self._reset = 0.0
        self._blocked_until = 0.0
        self._in_flight = 0
        self._observed = False
        self._backoff = 2.0
        self._max_in_flight = max_in_flight

    @staticmethod
    def retry_time(headers: dict, now: float) -> float:
        hints = []
        reset = headers.get("x-ratelimit-reset")
        if reset is not None:
            try:
                value = float(reset)
                if value > 1e12:
                    value /= 1000
                if now < value < now + 7 * 86400:
                    hints.append(value)
            except (ValueError, TypeError):
                pass
        retry = headers.get("retry-after")
        if retry is not None:
            try:
                value = now + max(0, float(retry))
            except (TypeError, ValueError):
                try:
                    value = parsedate_to_datetime(str(retry)).timestamp()
                except (TypeError, ValueError, OverflowError):
                    value = now
            if now < value < now + 7 * 86400:
                hints.append(value)
        return max(hints, default=now)

    async def acquire(self) -> None:
        async with self._condition:
            while True:
                now = time.time()
                if self._reset and now >= self._reset + 1:
                    self._remaining, self._reset = None, 0.0
                    self._observed = False
                if self._blocked_until and now >= self._blocked_until:
                    self._blocked_until = 0.0
                    self._observed = False
                    if not self._reset:
                        self._remaining = None
                blocked = self._blocked_until > now
                exhausted = self._remaining is not None and self._remaining <= 2
                first_in_flight = not self._observed and self._in_flight > 0
                at_capacity = self._max_in_flight is not None and self._in_flight >= self._max_in_flight
                if not blocked and not exhausted and not first_in_flight and not at_capacity:
                    self._in_flight += 1
                    if self._remaining is not None:
                        self._remaining -= 1
                    return
                if exhausted and not self._blocked_until:
                    self._blocked_until = self._reset + 1 if self._reset > now else now + self._backoff
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=0.25)
                except asyncio.TimeoutError:
                    pass

    async def observe(self, headers: dict | None = None, status: int | None = None) -> None:
        headers = {str(key).lower(): value for key, value in (headers or {}).items()}
        async with self._condition:
            self._in_flight = max(0, self._in_flight - 1)
            self._observed = True
            now = time.time()
            try:
                reset = float(headers.get("x-ratelimit-reset", 0))
                reset = reset / 1000 if reset > 1e12 else reset
                remaining = max(0, int(headers["x-ratelimit-remaining"]))
                if now < reset < now + 7 * 86400 and reset >= self._reset:
                    available = max(0, remaining - self._in_flight)
                    self._remaining = available if reset > self._reset or self._remaining is None else min(self._remaining, available)
                    self._reset = reset
            except (KeyError, TypeError, ValueError):
                pass
            if status == 429:
                retry_at = self.retry_time(headers, now)
                self._blocked_until = max(self._blocked_until, retry_at + 1 if retry_at > now else now + self._backoff)
                self._backoff = min(60, self._backoff * 2)
            elif status is not None and 200 <= status < 300:
                self._backoff = 2.0
            self._condition.notify_all()
