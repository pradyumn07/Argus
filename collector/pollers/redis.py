"""
Redis poller (Upstash — one poller per database).

Real SLIs from INFO:
  - latency        : timed PING (below)
  - conn_pct       : connected_clients / maxclients
  - ops_sec        : instantaneous_ops_per_sec (INFO reports this directly)
  - err_rate       : delta of rejected_connections / delta of total_connections
  - storage_pct    : used_memory / maxmemory (falls back to soft cap)
  - cache_hit_ratio: keyspace_hits / (hits + misses) — real cache signal
"""

import redis.asyncio as aioredis

from classify import MetricSample
from config import STORAGE_CAP_BYTES
from pollers.base import Poller


class RedisPoller(Poller):
    def __init__(self, instance_id: str, url: str):
        super().__init__(instance_id, "redis")
        self.url = url
        self._client: aioredis.Redis | None = None

    def _redis(self) -> aioredis.Redis:
        if self._client is None:
            # rediss:// URLs (Upstash) carry TLS automatically
            self._client = aioredis.from_url(
                self.url, socket_timeout=8, decode_responses=True
            )
        return self._client

    async def _collect(self) -> MetricSample:
        r = self._redis()
        await r.ping()  # latency probe
        info = await r.info()  # merged INFO across sections

        connected = int(info.get("connected_clients", 0))
        maxclients = int(info.get("maxclients", 10000)) or 10000

        used_mem = int(info.get("used_memory", 0))
        max_mem = int(info.get("maxmemory", 0)) or STORAGE_CAP_BYTES["redis"]

        ops_sec = float(info.get("instantaneous_ops_per_sec", 0))

        hits = int(info.get("keyspace_hits", 0))
        misses = int(info.get("keyspace_misses", 0))
        cache_hit = (hits / (hits + misses)) if (hits + misses) else 1.0

        rejected = int(info.get("rejected_connections", 0))
        total_conns = int(info.get("total_connections_received", 0))
        rej_sec = self._delta_rate("rejected", rejected)
        conn_sec = self._delta_rate("total_conns", total_conns)
        if conn_sec and conn_sec > 0 and rej_sec is not None:
            err_rate = min(1.0, rej_sec / conn_sec)
        else:
            err_rate = 0.0

        sample = MetricSample(instance_id=self.instance_id, engine="redis")
        sample.conn_pct = connected / maxclients
        sample.ops_sec = ops_sec
        sample.err_rate = err_rate
        sample.cache_hit_ratio = cache_hit
        sample.storage_pct = used_mem / max_mem
        return sample

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
