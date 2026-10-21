"""Injectable clock so time-dependent logic (budgets, retries, expiry) is testable."""
from __future__ import annotations
import asyncio, time
from datetime import datetime, timezone


class Clock:
    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000

    def mono_ms(self) -> float:
        return time.monotonic() * 1000.0

    async def sleep(self, ms: float) -> None:
        await asyncio.sleep(ms / 1000.0)


class FakeClock(Clock):
    def __init__(self, start_ms: int = 1_792_400_000_000):
        self._t = start_ms
        self.slept: list[float] = []

    def now_ms(self) -> int:
        return self._t

    def mono_ms(self) -> float:
        return float(self._t)

    def advance(self, ms: float) -> None:
        self._t += int(ms)

    async def sleep(self, ms: float) -> None:
        self.slept.append(ms)
        self._t += int(ms)


def format_ts(ms: int) -> str:
    dt = datetime.fromtimestamp(ms // 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms % 1000:03d}Z"


def parse_ts(s: str) -> int:
    dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000 + dt.microsecond // 1000
