"""
Seed the `instances` table with metadata for every configured target.
Run once after applying schema.sql:

    cd collector && python ../seed/seed_instances.py
"""

import asyncio
import os
import sys

import asyncpg
from dotenv import load_dotenv

# reuse the same fleet wiring the collector uses
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collector"))
from fleet import build_fleet  # noqa: E402

load_dotenv()


async def main():
    dsn = os.environ["STORE_DSN"]
    conn = await asyncpg.connect(dsn)
    fleet = build_fleet()
    for meta, _poller in fleet:
        await conn.execute(
            """
            INSERT INTO instances (id, engine, provider, region, poll_interval_s)
            VALUES ($1,$2,$3,$4,$5)
            ON CONFLICT (id) DO UPDATE SET
              engine = excluded.engine,
              provider = excluded.provider,
              region = excluded.region,
              poll_interval_s = excluded.poll_interval_s
            """,
            meta["id"], meta["engine"], meta["provider"],
            meta["region"], meta["poll_interval_s"],
        )
        print(f"seeded {meta['id']}")
    await conn.close()
    print(f"done — {len(fleet)} instance(s)")


if __name__ == "__main__":
    asyncio.run(main())
