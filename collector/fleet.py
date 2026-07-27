"""
Fleet definition — builds the list of pollers from environment variables.

Secrets (connection strings) live ONLY in the environment, never in the DB.
The `instances` table holds metadata; this module holds the wiring.

Only targets whose env vars are present get polled, so you can start with
3 real instances and grow to 6 without touching code.

Recommended 6-target fleet (edit freely):
  pg-neon        NEON_DSN
  pg-supa        SUPA_DSN
  mongo-orders   MONGO_URI  db=orders
  mongo-catalog  MONGO_URI  db=catalog
  redis-cache    REDIS_CACHE_URL
  redis-session  REDIS_SESSION_URL
"""

import os

from pollers.postgres import PostgresPoller
from pollers.mongo import MongoPoller
from pollers.redis import RedisPoller

# (instance_id, engine, provider, region, interval_s, factory)
# factory returns a Poller or None if its env var is missing.
_DEFS = [
    ("pg-neon", "postgres", "neon", "us-east", 4,
     lambda: PostgresPoller("pg-neon", os.environ["NEON_DSN"]) if os.getenv("NEON_DSN") else None),
    ("pg-supa", "postgres", "supabase", "us-east", 4,
     lambda: PostgresPoller("pg-supa", os.environ["SUPA_DSN"]) if os.getenv("SUPA_DSN") else None),
    ("mongo-orders", "mongo", "atlas", "us-east", 3,
     lambda: MongoPoller("mongo-orders", os.environ["MONGO_URI"], "orders") if os.getenv("MONGO_URI") else None),
    ("mongo-catalog", "mongo", "atlas", "us-east", 3,
     lambda: MongoPoller("mongo-catalog", os.environ["MONGO_URI"], "catalog") if os.getenv("MONGO_URI") else None),
    ("redis-cache", "redis", "upstash", "us-east", 2,
     lambda: RedisPoller("redis-cache", os.environ["REDIS_CACHE_URL"]) if os.getenv("REDIS_CACHE_URL") else None),
    ("redis-session", "redis", "upstash", "us-east", 2,
     lambda: RedisPoller("redis-session", os.environ["REDIS_SESSION_URL"]) if os.getenv("REDIS_SESSION_URL") else None),
]


def build_fleet():
    """Return [(meta_dict, poller), ...] for every configured target."""
    fleet = []
    for iid, engine, provider, region, interval, factory in _DEFS:
        poller = factory()
        if poller is None:
            continue
        meta = {
            "id": iid, "engine": engine, "provider": provider,
            "region": region, "poll_interval_s": interval,
        }
        fleet.append((meta, poller))
    return fleet
