# CLAUDE.md — Project context for Claude Code

## What this is
**Argus** — a portfolio project: a real observability stack (Prometheus,
Mimir, Loki, Tempo, Grafana) over a heterogeneous free-tier database fleet
(Postgres, MongoDB, Redis), with an AIOps layer — statistical anomaly
detection plus a LangGraph/Claude agent doing root-cause analysis — on top.
Fully containerized, deployed to a local Kubernetes (`kind`) cluster. Full
rationale and phase-by-phase plan: see `BUILD_PLAN.md` (also in this repo)
— read that before making architectural decisions.

Built for: portfolio/interview leverage (SRE + agentic AI + full-stack),
targeting SRE fresher interviews (Calix). Every design choice should stay
defensible in an interview — no unexplained magic numbers, no unnecessary
paid services (must run on $0 infra except Claude API calls; Kubernetes
means a local `kind` cluster, not a paid managed one).

## Current status: Phase 2 complete and verified
The collector is a Prometheus exporter (`collector/exporter.py`), and
`docker-compose.yml` stands up the local DB fleet + collector → Prometheus
→ Mimir → Grafana. Verified end-to-end against 11 live targets: metrics
flow to Mimir, both dashboards render, and the chaos demo drives a real
healthy → critical → healthy transition on local Postgres and MySQL.

The fleet is **hybrid**: 5 local containers from Docker Hub images (always
available, reproducible, ~2-10ms latency) plus free-tier cloud instances
that are skipped automatically when their env vars aren't set. Targets are
declared in `deploy/fleet.yaml` — adding a database is a YAML edit plus a
compose service, never a code change.

Next: Phase 3 (logs + traces) per `BUILD_PLAN.md`.

## Repo map
```
collector/
  config.py       SLO thresholds per engine (postgres/mysql/mongo/redis) —
                  the source of truth for "healthy". Documented rationale
                  inline. slo_for() applies per-target overrides.
  classify.py     Turns a raw MetricSample into healthy/warning/critical +
                  a human-readable reason. Engine-agnostic.
  fleet.py        Loads deploy/fleet.yaml, interpolates ${ENV_VAR}, skips
                  targets whose secrets aren't set, builds pollers. Adding a
                  target is a YAML edit, not a code change.
  main.py         Supervisor: one independent asyncio loop per instance,
                  jittered interval, records into the exporter.
  exporter.py     Prometheus gauges (argus_*) + :9100/metrics HTTP server.
                  A passive pull exporter — Prometheus scrapes on its own
                  cadence, independent of each instance's poll interval.
  Dockerfile      Builds the collector image (build context = repo root).
  pollers/
    base.py       Shared contract: times the round-trip, tracks previous
                  cumulative counters for delta rates (per-key timestamps),
                  never crashes the collector on an unreachable target.
    postgres.py   Reads pg_stat_activity / pg_stat_database.
    mysql.py      Reads SHOW GLOBAL STATUS / information_schema.
    mongo.py      Reads serverStatus / dbStats.
    redis.py      Reads INFO.
loadgen/
  generate.py     Reads the same fleet.yaml. `list` shows targets; `baseline`
                  keeps metrics moving (per-target supervision, so a dead
                  target can't kill the run); `chaos <target>` sizes its
                  connection flood off the target's own max_connections.
deploy/
  fleet.yaml                   THE fleet definition — add databases here.
  prometheus/prometheus.yml    scrape collector:9100, remote_write to Mimir.
  mimir/mimir.yaml             Mimir single-binary mode, filesystem storage.
  grafana/provisioning/        datasource (uid argus-mimir) + dashboard
                               provider, as code.
  grafana/dashboards/          argus-fleet.json (overview, status timeline,
                               all six SLIs) + argus-instance.json (per-
                               instance drill-down with $instance variable).
docker-compose.yml   local DB fleet + collector + prometheus + mimir +
                     grafana, one command.
.env.example      Connection strings for the OPTIONAL hosted targets only.
requirements.txt  asyncpg, aiomysql, motor, redis, python-dotenv,
                  prometheus-client, PyYAML
BUILD_PLAN.md     Full architecture + phase plan — read before architecture
                  decisions.
```

## Running it
`docker compose up --build` is the whole thing — the local fleet needs no
credentials. Grafana `localhost:3000`, Prometheus `localhost:9090`,
collector `localhost:9100/metrics`.

To keep metrics moving and demo a breach:
```
pip install -r requirements.txt
cd loadgen && python generate.py baseline        # leave running
python generate.py chaos pg-local --seconds 40   # watch the timeline
```

### Adding a database
1. Add a service to `docker-compose.yml` (official image, healthcheck,
   published port on the 5xxxx range to avoid host collisions).
2. Add a target to `deploy/fleet.yaml`.
3. `docker compose up -d` — the collector picks it up. `loadgen` gets it
   automatically; add its host-port mapping to `HOST_PORT_MAP` if local.

A new *engine* additionally needs a poller subclassing `pollers/base.py`,
an SLO block in `config.py`, and a `STORAGE_CAP_BYTES` entry.

## Known state of the hosted targets
`pg-supa` (DNS fails — project likely deleted), `redis-cache` (bad
password), and `redis-session` (still the `yyy.upstash.io` placeholder) are
unconfigured. They're skipped or reported unreachable and do not block
anything; fix or delete them from `fleet.yaml` when convenient.

## Conventions to keep
- Secrets live ONLY in `.env` locally (or a k8s `Secret` once we're on
  Kubernetes), never committed anywhere. The fixed credentials for local
  containers in `fleet.yaml`/`docker-compose.yml` are NOT secrets — they're
  throwaway logins on a private compose network — but anything hosted must
  go through `${ENV_VAR}`.
- Every poller subclasses `pollers/base.py` and only implements `_collect()`.
- SLO thresholds are engine-specific and documented — don't add a threshold
  without a one-line rationale comment (this is interview material). The
  same applies to per-target `slo:` overrides in `fleet.yaml`; they exist
  because a container on localhost and a cross-region free-tier instance
  can't share a latency budget.
- Targets are declared in `deploy/fleet.yaml`, never hardcoded in Python.
  `loadgen` reads the same file, so there is exactly one fleet definition.
- Independent per-instance poll intervals are deliberate (real fleets don't
  poll in lockstep) — don't refactor this into a single shared loop, even
  when adding the Prometheus exporter, tracing, or anything else on top.
- Grafana datasources/dashboards, Prometheus/Mimir config, and (later) Helm
  values are all provisioned as code under `deploy/` — no click-ops.

## Style preferences
Prefer complete, ready-to-run code over partial snippets. Keep explanations
concise and direct — skip preamble, get to the point.
