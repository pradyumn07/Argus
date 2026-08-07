"""
MySQL poller.

Real SLIs from native status counters:
  - latency        : timed in base.poll() (a SELECT 1 round-trip below)
  - conn_pct       : Threads_connected / max_connections
  - ops_sec        : delta of Queries
  - err_rate       : delta Aborted_connects / delta Connections
  - cache_hit_ratio: InnoDB buffer pool hits / read requests
  - storage_pct    : SUM(data_length + index_length) for this schema / soft cap

aiomysql takes discrete connection params rather than a URL, so the DSN is
parsed here — mysql://user:pw@host:port/db.
"""

from urllib.parse import unquote, urlparse

import aiomysql

from classify import MetricSample
from config import STORAGE_CAP_BYTES
from pollers.base import Poller

# Status/variable names pulled in one pass each, then looked up by key.
_STATUS_KEYS = (
    "Threads_connected",
    "Queries",
    "Aborted_connects",
    "Connections",
    "Innodb_buffer_pool_read_requests",
    "Innodb_buffer_pool_reads",
)


class MySQLPoller(Poller):
    def __init__(self, instance_id: str, dsn: str):
        super().__init__(instance_id, "mysql")
        self.dsn = dsn
        self._conn = None

    def _params(self) -> dict:
        u = urlparse(self.dsn)
        return {
            "host": u.hostname or "localhost",
            "port": u.port or 3306,
            "user": unquote(u.username) if u.username else "root",
            "password": unquote(u.password) if u.password else "",
            "db": (u.path or "/").lstrip("/") or None,
            "connect_timeout": 8,
            "autocommit": True,
        }

    async def _ensure(self):
        if self._conn is None or self._conn.closed:
            self._conn = await aiomysql.connect(**self._params())
        return self._conn

    async def _collect(self) -> MetricSample:
        conn = await self._ensure()
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1")  # this is the latency probe
            await cur.fetchone()

            await cur.execute("SHOW GLOBAL STATUS")
            status = {k: v for k, v in await cur.fetchall() if k in _STATUS_KEYS}

            await cur.execute("SHOW VARIABLES LIKE 'max_connections'")
            row = await cur.fetchone()
            max_conn = int(row[1]) if row else 151  # MySQL's own default

            await cur.execute(
                """
                SELECT COALESCE(SUM(data_length + index_length), 0)
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                """
            )
            size_row = await cur.fetchone()
            db_bytes = int(size_row[0]) if size_row and size_row[0] is not None else 0

        def stat(key: str) -> int:
            try:
                return int(status.get(key, 0))
            except (TypeError, ValueError):
                return 0

        connected = stat("Threads_connected")
        ops_sec = self._delta_rate("queries", stat("Queries"))

        aborted_sec = self._delta_rate("aborted", stat("Aborted_connects"))
        conns_sec = self._delta_rate("conns", stat("Connections"))
        if conns_sec and conns_sec > 0 and aborted_sec is not None:
            err_rate = min(1.0, aborted_sec / conns_sec)
        else:
            err_rate = 0.0

        requests = stat("Innodb_buffer_pool_read_requests")
        disk_reads = stat("Innodb_buffer_pool_reads")
        cache_hit = ((requests - disk_reads) / requests) if requests else 1.0

        sample = MetricSample(instance_id=self.instance_id, engine="mysql")
        sample.conn_pct = connected / max_conn if max_conn else 0.0
        sample.ops_sec = ops_sec
        sample.err_rate = err_rate
        sample.cache_hit_ratio = max(0.0, min(1.0, cache_hit))
        sample.storage_pct = db_bytes / STORAGE_CAP_BYTES["mysql"]
        return sample

    async def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()
