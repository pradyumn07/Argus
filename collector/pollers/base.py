"""Base poller: shared latency timing + delta-counter bookkeeping."""

import time
from abc import ABC, abstractmethod

from classify import MetricSample


class Poller(ABC):
    """One long-lived poller per tracked instance.

    Holds previous cumulative counters between polls so throughput and
    error-rate deltas can be computed (native stats are cumulative).
    """

    def __init__(self, instance_id: str, engine: str):
        self.instance_id = instance_id
        self.engine = engine
        self._prev: dict[str, float] = {}
        self._prev_ts: float | None = None

    def _delta_rate(self, key: str, value: float) -> float | None:
        """Per-second rate of a cumulative counter since the last poll."""
        now = time.monotonic()
        prev = self._prev.get(key)
        prev_ts = self._prev_ts
        self._prev[key] = value
        if prev is None or prev_ts is None:
            self._prev_ts = now
            return None
        elapsed = now - prev_ts
        self._prev_ts = now
        if elapsed <= 0:
            return None
        d = value - prev
        return max(0.0, d / elapsed)

    async def poll(self) -> MetricSample:
        """Time the round-trip and gather native stats. Never raises."""
        t0 = time.perf_counter()
        try:
            sample = await self._collect()
            sample.latency_ms = (time.perf_counter() - t0) * 1000
            return sample
        except Exception as exc:  # unreachable / auth / timeout
            return MetricSample(
                instance_id=self.instance_id,
                engine=self.engine,
                unreachable=True,
                status_reason=f"unreachable: {type(exc).__name__}",
            )

    @abstractmethod
    async def _collect(self) -> MetricSample:
        """Engine-specific stat collection. Sets everything but latency_ms."""
        ...

    async def close(self) -> None:  # optional cleanup
        pass
