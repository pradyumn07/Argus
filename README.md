# Argus — Phase 1: Collector + Metrics Store

The always-on heartbeat of the fleet. Polls a heterogeneous set of free-tier
databases (Postgres, MongoDB, Redis), extracts real SLIs from each engine's
native stats, classifies them against engine-appropriate SLOs, and streams
normalized rows into a central metrics store.

```
collector/          async collector, one poller per instance
  config.py         SLO thresholds (the source of truth for "healthy")
  classify.py       normalized MetricSample + SLO evaluation
  store.py          writes samples to Supabase
  fleet.py          builds pollers from env (secrets never touch the DB)
  main.py           supervisor: independent poll loop per instance
  pollers/          postgres.py · mongo.py · redis.py
seed/
  schema.sql        run this on Supabase first
  seed_instances.py registers instance metadata
loadgen/
  generate.py       baseline traffic + on-demand chaos (your demo trigger)
```

## What each engine actually reports

| SLI | Postgres | Mongo | Redis |
|---|---|---|---|
| latency | timed `SELECT 1` | timed `ping` | timed `PING` |
| conn_pct | active / max_connections | connections.current / avail | clients / maxclients |
| ops_sec | Δ commits+rollbacks | Δ opcounters | `instantaneous_ops_per_sec` |
| err_rate | rollback ratio | Δ asserts / Δ ops | Δ rejected / Δ conns |
| cache_hit | blks_hit ratio | — | keyspace hit ratio |
| storage_pct | db_size / cap | dataSize / cap | used_memory / maxmemory |

Every value is a real signal pulled from the engine — nothing is faked. That's
the whole point: the grid reflects the actual state of real cloud databases.

## Setup

1. **Provision** the free-tier targets (see the build plan): Neon, Supabase,
   Atlas M0, two Upstash Redis DBs. Start with whatever you have — the collector
   polls only what's configured.
2. **Create the store schema:** open Supabase → SQL Editor → paste `seed/schema.sql` → run.
3. **Enable Realtime** on the `metrics` table (Supabase → Database → Replication) — needed in Phase 2/3.
4. `cp .env.example .env` and fill in every connection string you have.
5. Install deps:
   ```bash
   pip install -r requirements.txt
   ```
6. Register instances:
   ```bash
   cd collector && python ../seed/seed_instances.py
   ```

## Run

```bash
cd collector && python main.py
```
You'll see a live line per poll:
```
Argus collector online — 6 target(s): pg-neon, pg-supa, mongo-orders, ...
[pg-neon       ] healthy  14.2ms   within slo
[redis-cache   ] healthy  2.1ms    within slo
```

In a second terminal, keep metrics alive:
```bash
cd loadgen && python generate.py baseline
```

## Demo the failure path

Trigger a spike on one target and watch its status flip:
```bash
cd loadgen && python generate.py chaos pg-neon --seconds 30
```
The collector will log `warning` → `critical` with a reason like
`latency 210.4ms (1.4x slo)` or `connections 0.93 (1.0x slo)`, and the row lands
in `metrics`. This is the exact moment you'll capture for the demo video.

## Notes on the SLOs

Thresholds live in `collector/config.py`. They're set per engine on purpose:
Redis (in-memory) has a ~10ms latency budget, Postgres (OLTP) ~50ms, Mongo ~40ms;
saturation/error/capacity budgets are shared because they express the same
operational risk. Tune them to your real baselines before demoing — status
colors are only defensible if the thresholds are.

## Next: Phase 2

Stand up the FastAPI backend, expose `/fleet` and `/instance/{id}/history`,
and let the frontend subscribe to Supabase Realtime for live pushes.
