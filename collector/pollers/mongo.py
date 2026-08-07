"""
MongoDB poller (Atlas M0 — one poller per logical database).

Real SLIs:
  - latency        : timed round-trip of a ping (below)
  - conn_pct       : connections.current / (current + available)  (serverStatus)
  - ops_sec        : delta of total opcounters
  - err_rate       : delta of metrics.commands[*].failed / delta of [*].total
  - storage_pct    : dbStats().dataSize / soft cap
  - cache_hit_ratio: not exposed cleanly on M0 → left None

err_rate deliberately does NOT use serverStatus().asserts. That counter
climbs continuously from internal housekeeping (session/cursor reaping,
connection churn from things like a Docker healthcheck reconnecting every
few seconds) with zero correlation to real command failures — verified by
watching asserts.user climb on an idle mongod with metrics.commands.*.failed
staying at 0 the whole time. metrics.commands is the actual per-command
success/failure ledger the server keeps for exactly this purpose.
"""

from motor.motor_asyncio import AsyncIOMotorClient

from classify import MetricSample
from config import STORAGE_CAP_BYTES
from pollers.base import Poller


def _sum_counters(doc: dict) -> int:
    """Total a serverStatus counter document.

    MongoDB 5.0+ nests sub-documents inside these counters (e.g.
    `opcounters.deprecated` is itself a dict of legacy opcodes), so summing
    the values blindly raises TypeError. Only the top-level numeric counters
    represent current traffic — skip anything nested.
    """
    return sum(int(v) for v in doc.values() if isinstance(v, (int, float)))


def _sum_command_totals(metrics_commands: dict) -> tuple[int, int]:
    """(total, failed) summed across every per-command counter.

    Each entry that tracks failures looks like {"failed": N, "total": N};
    a handful of keys (e.g. "<UNKNOWN>") don't and are skipped.
    """
    total = failed = 0
    for entry in metrics_commands.values():
        if isinstance(entry, dict) and "failed" in entry and "total" in entry:
            total += int(entry["total"])
            failed += int(entry["failed"])
    return total, failed


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

        total_ops = _sum_counters(ss.get("opcounters", {}))
        ops_sec = self._delta_rate("ops", total_ops)

        cmd_total, cmd_failed = _sum_command_totals(ss.get("metrics", {}).get("commands", {}))
        cmd_total_sec = self._delta_rate("cmd_total", cmd_total)
        cmd_failed_sec = self._delta_rate("cmd_failed", cmd_failed)

        if cmd_total_sec and cmd_total_sec > 0 and cmd_failed_sec is not None:
            err_rate = min(1.0, cmd_failed_sec / cmd_total_sec)
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
