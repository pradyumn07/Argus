"""
Postgres poller (Neon, Supabase).

Real SLIs from native catalogs:
  - latency        : timed in base.poll() (a SELECT 1 round-trip below)
  - conn_pct       : active backends / max_connections  (pg_stat_activity)
  - ops_sec        : delta of xact_commit + xact_rollback (pg_stat_database)
  - err_rate       : rollback ratio = rollbacks / (commits + rollbacks)
  - cache_hit_ratio: blks_hit / (blks_hit + blks_read)
  - storage_pct    : pg_database_size / soft cap
"""

import asyncpg

from classify import MetricSample
from config import STORAGE_CAP_BYTES
from pollers.base import Poller


class PostgresPoller(Poller):
    def __init__(self, instance_id: str, dsn: str):
        super().__init__(instance_id, "postgres")
        self.dsn = dsn
        self._conn: asyncpg.Connection | None = None

    async def _ensure(self) -> asyncpg.Connection:
        if self._conn is None or self._conn.is_closed():
            # asyncpg reads sslmode from the DSN query string (?sslmode=require)
            self._conn = await asyncpg.connect(self.dsn, timeout=8)
        return self._conn

    async def _collect(self) -> MetricSample:
        conn = await self._ensure()
        await conn.fetchval("SELECT 1")  # this is the latency probe

        row = await conn.fetchrow(
            """
            SELECT
              (SELECT count(*) FROM pg_stat_activity
                 WHERE state = 'active')                       AS active,
              current_setting('max_connections')::int          AS max_conn,
              d.xact_commit                                     AS commits,
              d.xact_rollback                                   AS rollbacks,
              d.blks_hit                                        AS hits,
              d.blks_read                                       AS reads,
              pg_database_size(current_database())              AS db_bytes
            FROM pg_stat_database d
            WHERE d.datname = current_database()
            """
        )

        active = row["active"] or 0
        max_conn = row["max_conn"] or 1
        commits = row["commits"] or 0
        rollbacks = row["rollbacks"] or 0
        hits = row["hits"] or 0
        reads = row["reads"] or 0
        db_bytes = row["db_bytes"] or 0

        total_txn = commits + rollbacks
        ops_sec = self._delta_rate("txn", total_txn)

        sample = MetricSample(instance_id=self.instance_id, engine="postgres")
        sample.conn_pct = active / max_conn
        sample.ops_sec = ops_sec
        sample.err_rate = (rollbacks / total_txn) if total_txn else 0.0
        sample.cache_hit_ratio = (hits / (hits + reads)) if (hits + reads) else 1.0
        sample.storage_pct = db_bytes / STORAGE_CAP_BYTES["postgres"]
        return sample

    async def close(self) -> None:
        if self._conn and not self._conn.is_closed():
            await self._conn.close()
