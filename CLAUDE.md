# CLAUDE.md — Project context for Claude Code

## What this is
**Argus** — a portfolio project: a real observability stack (Prometheus,
Mimir, Loki, Tempo, Grafana) over a heterogeneous database fleet (Postgres,
MySQL, MongoDB, Redis — local containers plus free-tier cloud), with an
AIOps layer on top: statistical anomaly detection plus a LangGraph agent
(Gemini free tier) doing root-cause analysis.
Fully containerized, deployed to a local Kubernetes (`kind`) cluster. Full
rationale and phase-by-phase plan: see `BUILD_PLAN.md` (also in this repo)
— read that before making architectural decisions.

Built for: portfolio/interview leverage (SRE + agentic AI + full-stack),
targeting SRE fresher interviews (Calix). Every design choice should stay
defensible in an interview — no unexplained magic numbers, no unnecessary
paid services (must run on $0 infra — Kubernetes means a local `kind`
cluster, not a paid managed one, and the agent uses a free-tier LLM API).

## Current status: Phases 1-4 complete, running on Kubernetes via ArgoCD
The full Grafana stack is live: **Mimir** (metrics), **Loki** (structured
JSON logs), **Tempo** (OTel traces), all three wired into Grafana with
bidirectional trace↔log correlation. Deployed to a local `kind` cluster and
reconciled by **ArgoCD** from `k8s/` on `master` — a git push is the only
deploy step.

Two things that bite in this repo, both already fixed but worth knowing:
- **ConfigMap changes don't roll pods by themselves.** `k8s/generate.py`
  stamps a content hash into each consuming pod template so they do. Always
  run it after editing anything under `deploy/`; never hand-edit the
  generated files in `k8s/configmaps/` or the hash annotations.
- **Locally-built images** (`argus-collector`, `argus-loadgen`, `argus-agent`) need
  `docker compose build` + `kind load docker-image ... --name argus`, then
  a rollout — ArgoCD can't build images, and the `:latest` tag means an
  unchanged tag won't re-pull.

**Phase 4 (AIOps) is in:** `agent/` runs a rolling-z-score anomaly detector
that triggers a LangGraph investigation (`recall -> investigate -> draft_rca
-> remember`) with PromQL/LogQL/TraceQL tools and two-tier memory.

The agent uses **Google Gemini's free tier**, not a paid API — Argus is now
$0 end to end, inference included. Without `GEMINI_API_KEY` the agent runs
detect-only (logs anomalies, skips RCA) rather than crash-looping, so the
manifest is safe to deploy before the key exists.

Next: Phase 5-6 polish per `BUILD_PLAN.md` — everything is already
containerized and on Kubernetes, so those are largely done ahead of order.

## Previous milestone: Phase 2 complete and verified
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
                  jittered interval, records into the exporter. Each poll
                  emits all three signals — metric, log, span — correlated
                  by trace_id (the log call sits INSIDE the span; moving it
                  out silently breaks correlation).
  exporter.py     Prometheus gauges (argus_*) + :9100/metrics HTTP server.
                  A passive pull exporter — Prometheus scrapes on its own
                  cadence, independent of each instance's poll interval.
  logs.py         Structured JSON to stdout, with trace_id from the active
                  span. Alloy tails stdout -> Loki.
  tracing.py      One OTel span per poll, OTLP/HTTP -> Tempo. No-op unless
                  OTEL_EXPORTER_OTLP_ENDPOINT is set.
  Dockerfile      Builds the collector image (build context = repo root).
  pollers/
    base.py       Shared contract: times the round-trip, tracks previous
                  cumulative counters for delta rates (per-key timestamps),
                  never crashes the collector on an unreachable target.
    postgres.py   Reads pg_stat_activity / pg_stat_database.
    mysql.py      Reads SHOW GLOBAL STATUS / information_schema.
    mongo.py      Reads serverStatus / dbStats.
    redis.py      Reads INFO.
agent/
  detector.py     Rolling z-score over PromQL. Scores the recent TAIL, not
                  just the newest point — a recovered spike is still an
                  incident. Complements classify.py, doesn't replace it.
  graph.py        LangGraph: recall -> investigate -> draft_rca -> remember.
                  Hand-written tool loop so each call is an OTel span.
  tools.py        4 tools: PromQL, LogQL, TraceQL, SLO context.
  slo.py          Reads collector/config.py + fleet.yaml so the agent judges
                  values against the SAME thresholds the collector uses.
  memory.py       Tier 1 = run state; tier 2 = incidents on disk.
  main.py         CLI: models | scan | investigate | watch | memory.
                  `scan` proves the telemetry path with zero token spend.
loadgen/
  generate.py     Reads the same fleet.yaml. `list` shows targets; `baseline`
                  keeps metrics moving (per-target supervision, so a dead
                  target can't kill the run); `chaos <target>` sizes its
                  connection flood off the target's own max_connections.
deploy/
  fleet.yaml                   THE fleet definition — add databases here.
  prometheus/prometheus.yml    scrape collector:9100, remote_write to Mimir.
  mimir/mimir.yaml             Mimir single-binary mode, filesystem storage.
  loki/loki.yaml               Loki single-binary, filesystem storage.
  tempo/tempo.yaml             Tempo single-binary, OTLP receivers on 4317/4318.
  alloy/config.alloy           log shipping, docker-compose (Docker socket).
  alloy/config-k8s.alloy       log shipping, Kubernetes (API-server based).
                               Two files because discovery genuinely differs;
                               everything downstream is identical.
  grafana/provisioning/        datasources (argus-mimir / argus-loki /
                               argus-tempo, with trace<->log links) +
                               dashboard provider, as code.
  grafana/dashboards/          argus-fleet.json (overview, status timeline,
                               all six SLIs) + argus-instance.json (per-
                               instance drill-down with $instance variable).
k8s/
  generate.py                  Regenerates every ConfigMap from deploy/ AND
                               stamps config hashes into pod templates.
                               RUN THIS after editing deploy/.
  create-secret.sh             Cloud DSNs from .env -> Secret. No values committed.
  configmaps/ db/ *.yaml       Raw manifests, synced by ArgoCD.
argocd/application.yaml        ArgoCD bootstrap (applied once, by hand).
                               Lives outside k8s/ on purpose.
docker-compose.yml   local DB fleet + collector + full LGTM stack + alloy,
                     one command. `--profile tools` adds loadgen.
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
- Grafana datasources/dashboards, Prometheus/Mimir/Loki/Tempo/Alloy config,
  and (later) Helm values are all provisioned as code under `deploy/` — no
  click-ops. `deploy/` is the single source; `k8s/configmaps/` is generated
  from it, never edited directly.
- The three signals are correlated by `trace_id`, which only works because
  the log call happens inside the active span. Keep it that way.
- Loki labels stay low-cardinality (`instance_id`, `engine`, `status`,
  `level`). Never promote `trace_id`, `latency_ms`, or anything unbounded
  to a label.

## Style preferences
Prefer complete, ready-to-run code over partial snippets. Keep explanations
concise and direct — skip preamble, get to the point.
