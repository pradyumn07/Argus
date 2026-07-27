-- Argus metrics store schema (run this on your Supabase Postgres project)
-- Supabase → SQL Editor → paste → run.

-- pgvector is used by the agent's incident memory (Phase 4). Safe to enable now.
create extension if not exists vector;

-- ─────────────────────────────────────────────────────────────
-- instances: metadata for each tracked target (NO secrets here)
-- ─────────────────────────────────────────────────────────────
create table if not exists instances (
    id              text primary key,          -- e.g. 'pg-neon'
    engine          text not null,             -- 'postgres' | 'mongo' | 'redis'
    provider        text not null,             -- 'neon' | 'supabase' | 'atlas' | 'upstash'
    region          text,
    poll_interval_s numeric not null default 4,
    created_at      timestamptz not null default now()
);

-- ─────────────────────────────────────────────────────────────
-- metrics: the time-series. One row per poll per instance.
-- ─────────────────────────────────────────────────────────────
create table if not exists metrics (
    id              bigint generated always as identity primary key,
    instance_id     text not null references instances(id) on delete cascade,
    ts              timestamptz not null default now(),
    latency_ms      numeric,                   -- null when unreachable
    conn_pct        numeric,                   -- active connections / max
    ops_sec         numeric,                   -- throughput (delta-derived)
    err_rate        numeric,                   -- engine-appropriate error proxy (0..1)
    cache_hit_ratio numeric,                   -- 0..1 (null for redis 'session' etc.)
    storage_pct     numeric,                   -- used / soft cap
    status          text not null,             -- 'healthy' | 'warning' | 'critical'
    status_reason   text                       -- which SLI drove the status
);

create index if not exists idx_metrics_instance_ts
    on metrics (instance_id, ts desc);

-- ─────────────────────────────────────────────────────────────
-- incidents: agent long-term memory (populated in Phase 5)
-- ─────────────────────────────────────────────────────────────
create table if not exists incidents (
    id          bigint generated always as identity primary key,
    instance_id text not null references instances(id) on delete cascade,
    opened_at   timestamptz not null default now(),
    closed_at   timestamptz,
    severity    text not null,                 -- 'warning' | 'critical'
    summary     text,
    root_cause  text,
    resolution  text,
    embedding   vector(1536)                   -- semantic recall over past incidents
);

-- Enable Realtime so the frontend gets live pushes (Phase 2/3):
--   Supabase Dashboard → Database → Replication → add `metrics` and `incidents`.
