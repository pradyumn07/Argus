# BUILD_PLAN.md — Argus architecture and phase plan

## Why this exists

`CLAUDE.md` points here for architectural decisions. This is the source of
truth for what Argus is becoming and why, phase by phase. Update it whenever
a phase's scope changes — don't let it drift from what's actually built.

## What Argus is

A portfolio project demonstrating real SRE + agentic-AI + full-stack skills:
a heterogeneous free-tier database fleet (Postgres, MongoDB, Redis) observed
through a real observability stack — **Prometheus, Mimir, Loki, Tempo,
Grafana** — with an **AIOps layer** (statistical anomaly detection + a
Claude/LangGraph agent doing root-cause analysis) on top, fully
containerized and deployed to a local **Kubernetes** cluster.

Every design choice must stay defensible in an interview: no unexplained
magic numbers, no paid infra, no tool added just to have its name on the
resume. Where a tool is genuinely overkill for this scale (see Mimir below),
say so honestly and explain why it's still the right choice for the
learning/demo goal.

Started as a simpler design (custom collector → Supabase Postgres → React
dashboard). That metrics pipeline has been fully replaced by the stack
below — see git history before this file existed for the old shape if
needed.

## Architecture

### The fleet: hybrid, declarative

Targets are declared in `deploy/fleet.yaml`, not hardcoded in Python. Adding
a database is a YAML entry plus (for local ones) a compose service; the file
is mounted into the collector, so no image rebuild is needed. `loadgen`
reads the same file, so there is exactly one fleet definition in the repo.

The fleet is deliberately hybrid:

- **Local containers** from official Docker Hub images (Postgres, MySQL,
  Mongo, two Redis). Always available, reproducible from a clean checkout,
  no accounts, and fast enough (~2-10ms) that the engine SLO budgets are
  meaningful as written.
- **Hosted free-tier instances** (Neon, Atlas, Upstash) referenced as
  `${ENV_VAR}`. A target whose variable is unset is skipped, so the stack
  runs fully without any cloud accounts. These prove the collector works
  against real multi-provider infrastructure, not just localhost.

Because those two classes have genuinely different performance envelopes,
SLOs are tiered: engine defaults live in `collector/config.py` and
individual targets widen theirs via a `slo:` block in `fleet.yaml`
(`slo_for()` merges them). A single global threshold would either flag every
cloud instance permanently or excuse a local one that's actually broken —
neither is defensible. Direction (`higher_is_worse`) is never overridable,
since that's a property of the SLI itself.

### Metrics: collector → Prometheus → Mimir → Grafana

The collector (`collector/`) is unchanged at its core: one independent
asyncio poll loop per instance (`main.py`), engine-specific pollers
(`pollers/postgres.py`, `mongo.py`, `redis.py` via `pollers/base.py`), SLO
classification (`classify.py` against `config.py`). This is deliberately
**not** refactored into a single shared loop — a real fleet doesn't poll in
lockstep, and independent jittered intervals are part of the design.

What changed: instead of writing each classified sample to Supabase
(`store.py`, now removed), `collector/exporter.py` holds the same data as
`prometheus_client` Gauges (`argus_latency_ms`, `argus_conn_pct`,
`argus_ops_sec`, `argus_err_rate`, `argus_cache_hit_ratio`,
`argus_storage_pct`, `argus_status_level`), labeled by
`instance_id, engine, provider, region`. The collector serves these on
`:9100/metrics` — a passive pull exporter, same pattern as
`postgres_exporter`: Prometheus decides when to scrape, the collector just
answers with whatever it last observed. This preserves the "poll on your
own independent cadence" design while still being a normal Prometheus
target.

Prometheus scrapes the collector and `remote_write`s to **Mimir** for
durable, queryable long-term storage. Honest note on Mimir: at 6 free-tier
targets, vanilla Prometheus's local TSDB would be functionally sufficient —
Mimir earns its place at scale (horizontal scale-out, multi-tenancy, long
retention across a large fleet). It's included here anyway because
Prometheus-scrapes-and-remote-writes-to-Mimir is exactly the pattern real
production setups (including Grafana Cloud) use, and being able to explain
*why* you'd reach for it — and that you understand it's not needed at this
scale — is itself interview material. Runs in Mimir's single-binary
"monolithic" mode (`-target=all`), not the multi-microservice
`mimir-distributed` — that split only matters once you need to scale
components independently.

Grafana queries Mimir as a Prometheus-compatible datasource. Datasources
and dashboards are provisioned as code (`deploy/grafana/provisioning/`,
committed dashboard JSON) — no click-ops, so the whole stack is
reproducible from a clean checkout.

### Logs: Loki

The collector emits one JSON object per poll on stdout (`collector/logs.py`)
— never a file, never a direct push to Loki, because the collector shouldn't
know Loki exists and both Docker and Kubernetes already solve "collect a
container's stdout" better than an in-process shipper would.

Grafana Alloy tails those streams and ships them to Loki (a DaemonSet in
Kubernetes). Alloy rather than Promtail simply because Promtail is EOL.
Log discovery is the one place the two runtimes genuinely differ — Docker
socket vs the Kubernetes API — so there are two Alloy configs under
`deploy/alloy/`; everything downstream (JSON parsing, label set, Loki
endpoint) is identical.

Only low-cardinality fields become Loki labels: `instance_id`, `engine`,
`status`, `level`. `trace_id` and `latency_ms` deliberately stay in the log
body — unbounded label values are the classic way to melt a Loki index, and
LogQL can still filter on body content.

### Traces: Tempo

One OpenTelemetry span per poll (`collector/tracing.py`), carrying the
instance and the SLO verdict it produced, exported OTLP/HTTP to Tempo. Off
entirely unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set, so the collector still
runs standalone against a bare fleet.

No OpenTelemetry Collector in front of Tempo — with one producer and one
backend it's a hop that can fail without buying anything, and Tempo speaks
OTLP natively. That calculus flips the moment the Phase 4 agent starts
emitting spans too.

**The correlation is the point.** Every log line carries the `trace_id` of
the span active when it was emitted, which is why the log call sits *inside*
the span in `poll_loop()`. Grafana turns that into a derived field linking
each log line to its trace, and Tempo's `tracesToLogsV2` links each span
back to the logs emitted during it. So: a latency spike on a Mimir panel →
the log line explaining it in Loki → the exact span in Tempo, three clicks,
all keyed off one poll event.

### AIOps: detector + agent

Two pieces, deliberately separate concerns:

1. **Anomaly detector** — a small service that runs PromQL range queries on
   a schedule and flags rolling z-score outliers per SLI. This is a cheap,
   fully explainable complement to `classify.py`'s static SLO breach
   (SLO breach = "this crossed a fixed line we decided on"; anomaly score =
   "this is behaving unlike its own recent history, even if still under the
   fixed line"). Deliberately not an LSTM/deep model — at this data volume
   a heavier model would be unjustifiable and impossible to explain in an
   interview.
2. **LangGraph/Claude agent** — has tools to query Prometheus/Mimir, Loki,
   and Tempo directly. Runs on-demand from a chat interface, and can also
   be triggered by an Alertmanager webhook when the detector fires, to
   produce an RCA summary automatically.

Open question, deferred to this phase: where the agent's long-term memory
(past incidents, for semantic recall) lives. The original design sketched a
Postgres + pgvector `incidents` table; that's still a reasonable choice but
will be designed fresh when this phase starts rather than carried forward
as speculative schema.

### Containers and Kubernetes

Each Argus-owned service (collector, anomaly-detector, agent) gets its own
`Dockerfile`. Everything else (Prometheus, Mimir, Loki, Tempo, Grafana,
OTel Collector, Alertmanager) uses official images, configured rather than
built.

Deployment target is a local `kind` cluster — $0, matches the existing
infra constraint, good enough to demo and screenshot. Namespace `argus`.
Helm values and any raw manifests live under `deploy/`. Secrets (DB
connection strings, `ANTHROPIC_API_KEY`) are created with `kubectl create
secret` from `.env` at deploy time and are never committed.

## Phases

1. **Phase 1 — done.** Collector + SLO classification against real
   free-tier databases. Unit-tested, byte-compiles, not yet the subject of
   this rewrite.
2. **Phase 2 — metrics pipeline. Done, verified against 11 live targets.**
   Collector became a Prometheus exporter; Prometheus + Mimir + Grafana via
   `docker-compose`; Supabase metrics path removed. Then extended: a
   declarative `deploy/fleet.yaml`, a hybrid local+cloud fleet from Docker
   Hub images, MySQL added as a fourth engine, per-target SLO overrides, and
   two provisioned dashboards (fleet overview with status timeline, plus a
   per-instance drill-down). Chaos demo verified driving a real
   healthy → critical → healthy transition on Postgres and MySQL.
3. **Phase 3 — logs + traces. Done, verified in both compose and kind.**
   Structured JSON logging (`collector/logs.py`) shipped to Loki by Grafana
   Alloy; one OpenTelemetry span per poll (`collector/tracing.py`) exported
   OTLP/HTTP straight to Tempo; Loki and Tempo wired into Grafana beside
   Mimir with bidirectional trace↔log links. Verified by resolving the same
   `trace_id` in both Tempo and Loki for a single poll event.
   Two deviations from the original sketch, both deliberate: Alloy instead
   of Promtail (Promtail is EOL), and no OpenTelemetry Collector in front
   of Tempo — see `collector/tracing.py` for when that changes.
4. **Phase 4 — AIOps.** Anomaly detector service (PromQL + rolling z-score);
   LangGraph agent with Prometheus/Loki/Tempo tools; Alertmanager webhook
   wiring so a detected anomaly can trigger an automatic RCA run. Agent
   memory/persistence design decided here.
5. **Phase 5 — containerize everything.** `Dockerfile` for every
   Argus-owned service; full stack (Argus services + LGTM stack) runs via
   one `docker-compose up`.
6. **Phase 6 — Kubernetes.** Port the compose stack to a local `kind`
   cluster: Helm values for the observability stack, manifests for Argus's
   own services, secrets handling, a deploy runbook in the README.

Each phase should be independently demoable and verified against the real
fleet before starting the next — don't stack unverified layers.

## Conventions carried forward

- Secrets live only in `.env` (local) or a k8s `Secret` (cluster), never in
  committed files or config maps.
- Every poller subclasses `pollers/base.py` and only implements
  `_collect()`.
- SLO thresholds are engine-specific and documented in `config.py` — don't
  add one without a one-line rationale.
- Independent per-instance poll intervals are deliberate — see "Metrics"
  above. Don't collapse them into a shared loop even when adding the
  exporter/tracing layers.
- Prefer provisioning-as-code (Grafana datasources/dashboards, Helm values)
  over manual setup, so the whole stack rebuilds from a clean checkout.
