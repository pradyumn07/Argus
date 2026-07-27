"""
Argus load + chaos generator.

Two jobs:
  1. Baseline traffic  — keeps metrics alive so the grid isn't flat-green.
  2. Chaos mode        — on demand, hammers ONE target so its SLIs breach and
                         the heatmap lights up. This is your demo trigger.

Usage:
    python generate.py baseline               # steady mixed load on all targets
    python generate.py chaos pg-neon          # spike one target for ~30s
    python generate.py chaos redis-cache --seconds 45

Baseline is safe to leave running. Chaos is intentionally aggressive but
short-lived and read/write-scoped to throwaway keys/collections/tables.
"""

import argparse
import asyncio
import os
import random
import time

import asyncpg
from motor.motor_asyncio import AsyncIOMotorClient
import redis.asyncio as aioredis
from dotenv import load_dotenv

load_dotenv()

PG_TARGETS = {
    "pg-neon": os.getenv("NEON_DSN"),
    "pg-supa": os.getenv("SUPA_DSN"),
}
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DBS = {"mongo-orders": "orders", "mongo-catalog": "catalog"}
REDIS_TARGETS = {
    "redis-cache": os.getenv("REDIS_CACHE_URL"),
    "redis-session": os.getenv("REDIS_SESSION_URL"),
}


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


async def run_baseline():
    stop = asyncio.Event()
    tasks = []
    for dsn in PG_TARGETS.values():
        if dsn:
            tasks.append(pg_baseline(dsn, stop))
    if MONGO_URI:
        for db_name in MONGO_DBS.values():
            tasks.append(mongo_baseline(MONGO_URI, db_name, stop))
    for url in REDIS_TARGETS.values():
        if url:
            tasks.append(redis_baseline(url, stop))
    if not tasks:
        raise SystemExit("No targets configured — set the same env vars as the collector.")
    print(f"baseline load running on {len(tasks)} target(s). Ctrl+C to stop.")
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        stop.set()


# ─────────────── chaos (aggressive, short-lived, single target) ───────────────
async def chaos_pg(dsn: str, seconds: int):
    # open many concurrent connections + run heavy scans → conn saturation + latency
    async def hog():
        c = await asyncpg.connect(dsn)
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            await c.fetch("SELECT count(*), pg_sleep(0.05) FROM generate_series(1, 50000)")
        await c.close()
    await asyncio.gather(*[hog() for _ in range(15)])


async def chaos_mongo(uri: str, db_name: str, seconds: int):
    col = AsyncIOMotorClient(uri, maxPoolSize=50)[db_name]["argus_load"]
    end = time.monotonic() + seconds

    async def hog():
        while time.monotonic() < end:
            # unindexed regex scan = expensive
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


async def run_chaos(target: str, seconds: int):
    print(f"CHAOS on {target} for ~{seconds}s — watch the grid light up.")
    if target in PG_TARGETS and PG_TARGETS[target]:
        await chaos_pg(PG_TARGETS[target], seconds)
    elif target in MONGO_DBS and MONGO_URI:
        await chaos_mongo(MONGO_URI, MONGO_DBS[target], seconds)
    elif target in REDIS_TARGETS and REDIS_TARGETS[target]:
        await chaos_redis(REDIS_TARGETS[target], seconds)
    else:
        raise SystemExit(f"Unknown or unconfigured target: {target}")
    print("chaos ended — target should recover to healthy within a few polls.")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="mode", required=True)
    sub.add_parser("baseline")
    c = sub.add_parser("chaos")
    c.add_argument("target")
    c.add_argument("--seconds", type=int, default=30)
    args = p.parse_args()

    if args.mode == "baseline":
        asyncio.run(run_baseline())
    else:
        asyncio.run(run_chaos(args.target, args.seconds))


if __name__ == "__main__":
    main()
