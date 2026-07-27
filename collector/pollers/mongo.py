"""
MongoDB poller (Atlas M0 — one poller per logical database).

Real SLIs:
  - latency        : timed round-trip of a ping (below)
  - conn_pct       : connections.current / (current + available)  (serverStatus)
  - ops_sec        : delta of total opcounters
  - err_rate       : delta of asserts (user + regular) / delta of ops
  - storage_pct    : dbStats().dataSize / soft cap
  - cache_hit_ratio: not exposed cleanly on M0 → left None
"""

from motor.motor_asyncio import AsyncIOMotorClient

from classify import MetricSample
from config import STORAGE_CAP_BYTES
from pollers.base import Poller


class MongoPoller(Poller):
    def __init__(self, instance_id: str, uri: str, db_name: str):
        super().__init__(instance_id, "mongo")
        self.uri = uri
        self.db_name = db_name
        self._client: AsyncIOMotorClient | None = None

    def _db(self):
        if self._client is None:
            self._client = AsyncIOMotorClient(self.uri, serverSelectionTimeoutMS=8000)
        return self._client[self.db_name]

    async def _collect(self) -> MetricSample:
        db = self._db()
        await db.command("ping")  # latency probe

        ss = await db.command("serverStatus")
        dbstats = await db.command("dbStats")

        conns = ss.get("connections", {})
        current = conns.get("current", 0)
        available = conns.get("available", 1)

        opc = ss.get("opcounters", {})
        total_ops = sum(int(v) for v in opc.values())
        ops_sec = self._delta_rate("ops", total_ops)

        asserts = ss.get("asserts", {})
        total_asserts = sum(int(v) for v in asserts.values())
        assert_sec = self._delta_rate("asserts", total_asserts)

        # error rate ~ asserts per op over the interval
        if ops_sec and ops_sec > 0 and assert_sec is not None:
            err_rate = min(1.0, assert_sec / ops_sec)
        else:
            err_rate = 0.0

        data_size = dbstats.get("dataSize", 0)

        sample = MetricSample(instance_id=self.instance_id, engine="mongo")
        sample.conn_pct = current / (current + available) if (current + available) else 0.0
        sample.ops_sec = ops_sec
        sample.err_rate = err_rate
        sample.storage_pct = data_size / STORAGE_CAP_BYTES["mongo"]
        return sample

    async def close(self) -> None:
        if self._client:
            self._client.close()
