# Argus — an observability platform for a heterogeneous database fleet

Polls a mixed fleet of databases (Postgres, MySQL, MongoDB, Redis), extracts
real SLIs from each engine's native stats, classifies them against
engine-appropriate SLOs, and feeds a real observability stack — Prometheus,
Mimir, Grafana, and (next) Loki, Tempo, and an AIOps layer. Full architecture
and phase plan: [`BUILD_PLAN.md`](BUILD_PLAN.md).

The fleet is **hybrid**: local containers from official Docker Hub images
(always available, reproducible) plus optional free-tier cloud instances.
Cloud targets are skipped automatically when their env vars aren't set, so
`docker compose up` works on a clean checkout with no accounts at all.

```
collector/          async collector, one independent poll loop per instance
  config.py         SLO thresholds + per-target overrides
  classify.py       normalized MetricSample + SLO evaluation
  exporter.py       Prometheus gauges + :9100/metrics
  fleet.py          loads deploy/fleet.yaml, resolves ${ENV_VAR}
  main.py           supervisor
  pollers/          postgres.py · mysql.py · mongo.py · redis.py
deploy/
  fleet.yaml        THE fleet definition — add databases here
  prometheus/       scrape + remote_write config
  mimir/            single-binary Mimir (long-term metrics storage)
  grafana/          provisioned datasource + dashboards (as code)
loadgen/
  generate.py       baseline traffic + on-demand chaos (your demo trigger)
docker-compose.yml  local DB fleet + collector + prometheus + mimir + grafana
```

## What each engine actually reports

| SLI | Postgres | MySQL | Mongo | Redis |
|---|---|---|---|---|
| latency | timed `SELECT 1` | timed `SELECT 1` | timed `ping` | timed `PING` |
| conn_pct | active / max_connections | Threads_connected / max_connections | connections.current / avail | clients / maxclients |
| ops_sec | Δ commits+rollbacks | Δ Queries | Δ opcounters | `instantaneous_ops_per_sec` |
| err_rate | rollback ratio | Δ Aborted_connects / Δ Connections | Δ asserts / Δ ops | Δ rejected / Δ conns |
| cache_hit | blks_hit ratio | InnoDB buffer pool | — | keyspace hit ratio |
| storage_pct | db_size / cap | data+index_length / cap | dataSize / cap | used_memory / maxmemory |

Every value is a real signal pulled from the engine — nothing is faked. Local
containers are real database servers reporting their own native stats; they're
just reproducible and fast instead of flaky and remote.

## Run

```bash
docker compose up --build
```

No credentials needed — the local fleet is self-contained. This brings up
five databases (Postgres, MySQL, Mongo, two Redis) plus:

- **collector** — polls the fleet, exposes `localhost:9100/metrics`
- **Prometheus** — `localhost:9090`, scrapes the collector, remote-writes to Mimir
- **Mimir** — `localhost:9009`, long-term metrics storage
- **Grafana** — `localhost:3000` (anonymous admin, local demo only)
  - **Argus Fleet** — overview stats, status timeline, all six SLIs, filterable by engine/provider
  - **Argus — Instance Detail** — per-instance drill-down via an `$instance` variable

To also monitor free-tier cloud instances, `cp .env.example .env` and fill in
whichever connection strings you have. Anything left unset is skipped.

Confirm it's working:
```bash
curl localhost:9100/metrics | grep argus_
```

## Generate load and demo a failure

```bash
pip install -r requirements.txt
cd loadgen
python generate.py list                          # what's configured
python generate.py baseline                      # leave this running
python generate.py chaos pg-local --seconds 40   # in another terminal
```

Chaos sizes its connection flood off the target's own `max_connections`, so
it breaches whether the target is a local container or a small cloud
instance. You'll see the status timeline go `healthy → critical → healthy`:

```
[pg-local      ] critical 4.3ms    connections 0.9 (1.0x slo)
[pg-local      ] warning  4.6ms    connections 0.9 (1.2x slo)
[pg-local      ] healthy  2.9ms    within slo
```

## Adding a database

1. Add a service to `docker-compose.yml` (official image + healthcheck).
2. Add a target to `deploy/fleet.yaml`.
3. `docker compose up -d`.

No Python changes and no image rebuild — `fleet.yaml` is mounted, and
`loadgen` reads the same file. A brand-new *engine* additionally needs a
poller subclassing `pollers/base.py`, an SLO block in `config.py`, and a
`STORAGE_CAP_BYTES` entry — `pollers/mysql.py` is the worked example.

## Notes on the SLOs

Thresholds live in `collector/config.py`, per engine and documented: Redis
(in-memory) ~10ms, Postgres/MySQL (OLTP) ~50ms, Mongo ~40ms; saturation,
error, and capacity budgets are shared because they express the same
operational risk.

Those are the **local** budgets. A cross-region free-tier instance can't meet
a localhost latency budget, so hosted targets widen theirs per target via the
`slo:` block in `deploy/fleet.yaml` — each with its own one-line rationale.
Tiering SLOs by where a target actually lives is the point; a single global
threshold would either flag every cloud instance forever or excuse a local
one that's genuinely broken.

## What's next

See [`BUILD_PLAN.md`](BUILD_PLAN.md): structured logs into Loki,
OpenTelemetry traces into Tempo, an anomaly detector + Claude/LangGraph RCA
agent, then a Kubernetes (`kind`) deployment.
