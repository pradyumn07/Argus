# CLAUDE.md — Project context for Claude Code

## What this is
**Argus** — a portfolio project: an agentic observability platform for a
heterogeneous free-tier database fleet (Postgres, MongoDB, Redis). A
LangGraph/Claude agent with real tools and memory answers natural-language
questions about fleet health; a real-time animated React dashboard shows a
live heatmap. Full rationale and phase-by-phase plan: see `BUILD_PLAN.md`
(also in this repo) — read that before making architectural decisions.

Built for: portfolio/interview leverage (SRE + agentic AI + full-stack),
targeting SRE fresher interviews (Calix). Every design choice should stay
defensible in an interview — no unexplained magic numbers, no unnecessary
paid services (must run on $0 infra except Claude API calls).

## Current status: Phase 1 complete, untested against real databases
Phase 1 (collector + metrics store) is code-complete and unit-tested
(classification logic verified, all files byte-compile). **It has not yet
been run against real provisioned databases** — that's the next concrete
step before touching Phase 2.

## Repo map
```
collector/
  config.py       SLO thresholds per engine (postgres/mongo/redis) — the
                  source of truth for "healthy". Documented rationale inline.
  classify.py     Turns a raw MetricSample into healthy/warning/critical +
                  a human-readable reason. Engine-agnostic.
  fleet.py        Builds the list of active pollers from env vars. Only
                  targets with env vars set get polled — safe to run with
                  a partial fleet.
  main.py         Supervisor: one independent asyncio loop per instance,
                  jittered interval, writes to the store.
  store.py        Thin writer: classified sample -> Supabase `metrics` row.
  pollers/
    base.py       Shared contract: times the round-trip, tracks previous
                  cumulative counters for delta rates, never crashes the
                  collector on an unreachable target.
    postgres.py   Reads pg_stat_activity / pg_stat_database.
    mongo.py      Reads serverStatus / dbStats.
    redis.py      Reads INFO.
seed/
  schema.sql          Run this in Supabase FIRST (instances/metrics/incidents
                      tables + pgvector extension for later agent memory).
  seed_instances.py   Registers instance metadata after schema.sql runs.
loadgen/
  generate.py     `baseline` keeps metrics moving; `chaos <target>` spikes
                  one instance on demand for demos.
.env.example      Every connection string the collector needs. Copy to .env.
requirements.txt  asyncpg, motor, redis, python-dotenv
```

## Immediate next step (do this before Phase 2)
1. Provision whatever subset of the 6-target fleet is ready (Neon, Supabase,
   Atlas M0, Upstash x2) — partial is fine, `fleet.py` only builds pollers
   for configured targets.
2. Run `seed/schema.sql` in Supabase, fill in `.env` from `.env.example`.
3. `pip install -r requirements.txt`, then `python seed/seed_instances.py`,
   then `cd collector && python main.py`.
4. Confirm real rows land in `metrics` with sensible status classifications.
5. Only then move to Phase 2 (FastAPI backend + Supabase Realtime wiring)
   per `BUILD_PLAN.md`.

## Conventions to keep
- Secrets live ONLY in `.env`, never in the `instances` table or committed
  anywhere.
- Every poller subclasses `pollers/base.py` and only implements `_collect()`.
- SLO thresholds are engine-specific and documented — don't add a threshold
  without a one-line rationale comment (this is interview material).
- Independent per-instance poll intervals are deliberate (real fleets don't
  poll in lockstep, and the frontend's pulse animation will key off actual
  poll events later) — don't refactor this into a single shared loop.

## Style preferences
Prefer complete, ready-to-run code over partial snippets. Keep explanations
concise and direct — skip preamble, get to the point.
