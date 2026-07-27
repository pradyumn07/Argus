"""Writes normalized samples into the Supabase metrics store."""

import asyncpg

from classify import MetricSample


class MetricStore:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=4)

    async def write(self, s: MetricSample) -> None:
        assert self._pool is not None, "call connect() first"
        await self._pool.execute(
            """
            INSERT INTO metrics (
                instance_id, latency_ms, conn_pct, ops_sec, err_rate,
                cache_hit_ratio, storage_pct, status, status_reason
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            """,
            s.instance_id, s.latency_ms, s.conn_pct, s.ops_sec, s.err_rate,
            s.cache_hit_ratio, s.storage_pct, s.status, s.status_reason,
        )

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
