"""
Argus load + chaos generator.

Two jobs:
  1. Baseline traffic  — keeps metrics alive so the dashboards aren't flat.
  2. Chaos mode        — on demand, hammers ONE target so its SLIs breach and
                         the status timeline lights up. This is your demo trigger.

Targets come from the same deploy/fleet.yaml the collector uses, so adding a
database there gives it load automatically — nothing to update here.

Usage:
    python generate.py list                   # show what's configured
    python generate.py baseline               # steady mixed load on all targets
    python generate.py chaos pg-local         # spike one target for ~30s
    python generate.py chaos redis-cache-local --seconds 45

Baseline is safe to leave running. Chaos is intentionally aggressive but
short-lived and read/write-scoped to throwaway keys/collections/tables.

Note: run this from the host, where fleet.yaml's container hostnames don't
resolve. Pass --host-ports to rewrite local targets onto their published
ports (pg-local:5432 -> localhost:55432, etc.), which is the default.
"""

import argparse
import asyncio
import os
import random
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import aiomysql
import asyncpg
from motor.motor_asyncio import AsyncIOMotorClient
import redis.asyncio as aioredis
from dotenv import load_dotenv

# reuse the collector's fleet loader so there is exactly one fleet definition
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))
from fleet import load_targets, _interpolate, _Unresolved  # noqa: E402

load_dotenv()

# Local compose services publish on shifted host ports (see docker-compose.yml)
# so they don't collide with anything already running on the standard ones.
HOST_PORT_MAP = {
    "pg-local": ("localhost", 55432),
    "mysql-local": ("localhost", 53306),
    "mongo-local": ("localhost", 57017),
    "redis-cache-local": ("localhost", 56379),
    "redis-session-local": ("localhost", 56380),
}


def _rewrite_for_host(dsn: str) -> str:
    """Point a compose-internal hostname at its published localhost port."""
    u = urlparse(dsn)
    mapped = HOST_PORT_MAP.get(u.hostname or "")
    if not mapped:
        return dsn
    host, port = mapped
    userinfo = ""
    if u.username:
        userinfo = u.username + (f":{u.password}" if u.password else "") + "@"
    rest = dsn.split("://", 1)[1]
    tail = rest.split("@", 1)[1] if "@" in rest else rest
    path_and_query = tail[len(tail.split("/")[0]):] if "/" in tail else ""
    return f"{u.scheme}://{userinfo}{host}:{port}{path_and_query}"


def resolve_fleet(host_ports: bool = True) -> dict:
    """{id: target_dict} for every target whose DSN resolves."""
    out = {}
    for t in load_targets():
        try:
            dsn = _interpolate(str(t["dsn"]))
        except _Unresolved:
            continue
        t = dict(t)
        t["dsn"] = _rewrite_for_host(dsn) if host_ports else dsn
        out[t["id"]] = t
    return out


# ─────────────── baseline workloads (light, continuous) ───────────────
async def pg_baseline(dsn: str, stop: asyncio.Event):
    conn = await asyncpg.connect(dsn)
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS argus_load "
        "(id serial primary key, v int, ts timestamptz default now())"
    )
    while not stop.is_set():
        await conn.execute("INSERT INTO argus_load (v) VALUES ($1)", random.randint(0, 999))
        await conn.fetch("SELECT v FROM argus_load ORDER BY id DESC LIMIT 20")
        await asyncio.sleep(random.uniform(0.3, 1.2))
    await conn.close()


def _mysql_params(dsn: str) -> dict:
    u = urlparse(dsn)
    return {
        "host": u.hostname or "localhost",
        "port": u.port or 3306,
        "user": unquote(u.username) if u.username else "root",
        "password": unquote(u.password) if u.password else "",
        "db": (u.path or "/").lstrip("/") or None,
        "autocommit": True,
    }


async def mysql_baseline(dsn: str, stop: asyncio.Event):
    conn = await aiomysql.connect(**_mysql_params(dsn))
    async with conn.cursor() as cur:
        await cur.execute(
            "CREATE TABLE IF NOT EXISTS argus_load "
            "(id INT AUTO_INCREMENT PRIMARY KEY, v INT, ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
    while not stop.is_set():
        async with conn.cursor() as cur:
            await cur.execute("INSERT INTO argus_load (v) VALUES (%s)", (random.randint(0, 999),))
            await cur.execute("SELECT v FROM argus_load ORDER BY id DESC LIMIT 20")
            await cur.fetchall()
        await asyncio.sleep(random.uniform(0.3, 1.2))
    conn.close()


async def mongo_baseline(uri: str, db_name: str, stop: asyncio.Event):
    col = AsyncIOMotorClient(uri)[db_name]["argus_load"]
    while not stop.is_set():
        await col.insert_one({"v": random.randint(0, 999), "ts": time.time()})
        await col.find().sort("ts", -1).limit(20).to_list(length=20)
        await asyncio.sleep(random.uniform(0.3, 1.2))


async def redis_baseline(url: str, stop: asyncio.Event):
    r = aioredis.from_url(url, decode_responses=True)
    i = 0
    while not stop.is_set():
        i += 1
        await r.set(f"argus:load:{i % 500}", random.randint(0, 999), ex=120)
        await r.get(f"argus:load:{random.randint(0, 600)}")  # some misses on purpose
        await asyncio.sleep(random.uniform(0.05, 0.3))
    await r.aclose()


async def _supervise(name: str, factory, stop: asyncio.Event):
    """Keep one target's baseline load alive without letting it kill the rest.

    An unreachable target (dead cloud instance, rotated password) must not
    take the whole load generator down with it — same principle as
    pollers/base.py never crashing the collector. Retries with backoff so a
    target that comes back gets load again.
    """
    delay = 5
    while not stop.is_set():
        try:
            await factory()
            return  # clean exit means stop was set
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[loadgen] {name}: {type(exc).__name__}: {exc} - retrying in {delay}s")
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                delay = min(delay * 2, 60)


async def run_baseline(fleet: dict):
    stop = asyncio.Event()
    builders = {
        "postgres": lambda t: lambda: pg_baseline(t["dsn"], stop),
        "mysql": lambda t: lambda: mysql_baseline(t["dsn"], stop),
        "mongo": lambda t: lambda: mongo_baseline(t["dsn"], t.get("database") or "argus", stop),
        "redis": lambda t: lambda: redis_baseline(t["dsn"], stop),
    }
    tasks = []
    for t in fleet.values():
        build = builders.get(t["engine"])
        if build:
            tasks.append(_supervise(t["id"], build(t), stop))
    if not tasks:
        raise SystemExit("No targets resolved from deploy/fleet.yaml.")
    print(f"baseline load running on {len(tasks)} target(s). Ctrl+C to stop.")
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except KeyboardInterrupt:
        stop.set()


# ─────────────── chaos (aggressive, short-lived, single target) ───────────────
async def chaos_pg(dsn: str, seconds: int):
    """Saturate connections + burn CPU.

    Sized off the target's own max_connections rather than a fixed number: a
    local container allows ~100 while a free-tier cloud instance allows far
    fewer, and a count tuned for one does nothing to the other. conn_pct
    counts *active* backends, so the holders run pg_sleep in a loop to stay
    active rather than idling.
    """
    probe = await asyncpg.connect(dsn)
    try:
        max_conn = int(await probe.fetchval("SELECT current_setting('max_connections')::int"))
    finally:
        await probe.close()

    # 92% of the ceiling clears the 90% critical threshold while leaving the
    # superuser-reserved slots free, so the collector itself can still connect.
    total = max(4, min(int(max_conn * 0.92), 120))
    burners = max(2, total // 10)
    end = time.monotonic() + seconds

    async def hold():
        try:
            c = await asyncpg.connect(dsn, timeout=10)
        except Exception:
            return
        try:
            while time.monotonic() < end:
                await c.fetch("SELECT pg_sleep(0.25)")
        except Exception:
            pass
        finally:
            await c.close()

    async def burn():
        try:
            c = await asyncpg.connect(dsn, timeout=10)
        except Exception:
            return
        try:
            while time.monotonic() < end:
                await c.fetch(
                    "SELECT count(*) FROM ("
                    "  SELECT md5(g::text) FROM generate_series(1, 400000) g ORDER BY 1"
                    ") s"
                )
        except Exception:
            pass
        finally:
            await c.close()

    print(f"  saturating {total}/{max_conn} connections ({burners} burning CPU)")
    await asyncio.gather(
        *[hold() for _ in range(total - burners)],
        *[burn() for _ in range(burners)],
        return_exceptions=True,
    )


async def chaos_mysql(dsn: str, seconds: int):
    """Same idea as chaos_pg, sized off MySQL's own max_connections."""
    probe = await aiomysql.connect(**_mysql_params(dsn))
    try:
        async with probe.cursor() as cur:
            await cur.execute("SHOW VARIABLES LIKE 'max_connections'")
            row = await cur.fetchone()
            max_conn = int(row[1]) if row else 151
    finally:
        probe.close()

    total = max(4, min(int(max_conn * 0.92), 120))
    burners = max(2, total // 10)
    end = time.monotonic() + seconds

    async def hold():
        try:
            c = await aiomysql.connect(**_mysql_params(dsn))
        except Exception:
            return
        try:
            while time.monotonic() < end:
                async with c.cursor() as cur:
                    await cur.execute("SELECT SLEEP(0.25)")
                    await cur.fetchall()
        except Exception:
            pass
        finally:
            c.close()

    async def burn():
        try:
            c = await aiomysql.connect(**_mysql_params(dsn))
        except Exception:
            return
        try:
            while time.monotonic() < end:
                async with c.cursor() as cur:
                    await cur.execute(
                        "SELECT COUNT(*) FROM information_schema.columns a, "
                        "information_schema.columns b"
                    )
                    await cur.fetchall()
        except Exception:
            pass
        finally:
            c.close()

    print(f"  saturating {total}/{max_conn} connections ({burners} burning CPU)")
    await asyncio.gather(
        *[hold() for _ in range(total - burners)],
        *[burn() for _ in range(burners)],
        return_exceptions=True,
    )


async def chaos_mongo(uri: str, db_name: str, seconds: int):
    col = AsyncIOMotorClient(uri, maxPoolSize=50)[db_name]["argus_load"]
    end = time.monotonic() + seconds

    async def hog():
        while time.monotonic() < end:
            # unindexed $where scan = expensive
            await col.find({"$where": "sleep(20) || true"}).limit(5).to_list(5)
    await asyncio.gather(*[hog() for _ in range(12)])


async def chaos_redis(url: str, seconds: int):
    r = aioredis.from_url(url, decode_responses=True)
    end = time.monotonic() + seconds

    # flood memory + fire a slow command repeatedly
    async def hog():
        i = 0
        while time.monotonic() < end:
            i += 1
            await r.set(f"argus:chaos:{i}", "x" * 2000)
            await r.keys("argus:chaos:*")  # O(N) — deliberately slow
    await asyncio.gather(*[hog() for _ in range(8)])
    await r.aclose()


async def run_chaos(fleet: dict, target: str, seconds: int):
    t = fleet.get(target)
    if not t:
        known = ", ".join(sorted(fleet)) or "(none)"
        raise SystemExit(f"Unknown or unconfigured target: {target}\nAvailable: {known}")

    print(f"CHAOS on {target} ({t['engine']}) for ~{seconds}s - watch the timeline light up.")
    engine, dsn = t["engine"], t["dsn"]
    if engine == "postgres":
        await chaos_pg(dsn, seconds)
    elif engine == "mysql":
        await chaos_mysql(dsn, seconds)
    elif engine == "mongo":
        await chaos_mongo(dsn, t.get("database") or "argus", seconds)
    elif engine == "redis":
        await chaos_redis(dsn, seconds)
    else:
        raise SystemExit(f"No chaos workload for engine {engine!r}")
    print("chaos ended - target should recover to healthy within a few polls.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--in-cluster",
        action="store_true",
        help="use compose-internal hostnames instead of published localhost ports",
    )
    sub = p.add_subparsers(dest="mode", required=True)
    sub.add_parser("list")
    sub.add_parser("baseline")
    c = sub.add_parser("chaos")
    c.add_argument("target")
    c.add_argument("--seconds", type=int, default=30)
    args = p.parse_args()

    fleet = resolve_fleet(host_ports=not args.in_cluster)

    if args.mode == "list":
        if not fleet:
            print("No targets resolved from deploy/fleet.yaml.")
            return
        width = max(len(i) for i in fleet)
        for iid, t in fleet.items():
            print(f"{iid:<{width}}  {t['engine']:<9} {t.get('provider', '?')}")
        return
    if args.mode == "baseline":
        asyncio.run(run_baseline(fleet))
    else:
        asyncio.run(run_chaos(fleet, args.target, args.seconds))


if __name__ == "__main__":
    main()
