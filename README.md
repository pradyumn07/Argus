# Argus — an observability platform for a heterogeneous database fleet

Polls a mixed fleet of databases (Postgres, MySQL, MongoDB, Redis), extracts
real SLIs from each engine's native stats, classifies them against
engine-appropriate SLOs, and feeds a real observability stack — Prometheus,
Mimir, Loki, Tempo, and Grafana — with an AIOps agent on top that detects
anomalies and writes the root-cause analysis itself. Runs on Kubernetes via
ArgoCD, and costs nothing to operate. Full architecture and phase plan:
[`BUILD_PLAN.md`](BUILD_PLAN.md); deep reference: [`ARCHITECTURE.md`](ARCHITECTURE.md).

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
agent/
  detector.py       rolling z-score anomaly detection over PromQL
  graph.py          LangGraph: recall -> investigate -> draft_rca -> remember
  tools.py          PromQL / LogQL / TraceQL / SLO-context tools
  memory.py         two-tier memory (working + episodic)
loadgen/
  generate.py       baseline traffic + on-demand chaos (your demo trigger)
k8s/                raw manifests, reconciled by ArgoCD
docker-compose.yml  local DB fleet + collector + full LGTM stack + agent
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

- **Loki** — `localhost:3100`, logs; **Alloy** tails every container into it
- **Tempo** — `localhost:3200`, traces; the collector posts OTLP to `:4318`

All three are wired into Grafana as datasources, with links between them —
a log line jumps to its trace, a trace jumps back to the instance's metrics.

To also monitor free-tier cloud instances, `cp .env.example .env` and fill in
whichever connection strings you have. Anything left unset is skipped.

Confirm it's working:
```bash
curl localhost:9100/metrics | grep argus_
```

### The live UI

```bash
docker compose --profile ui up ui        # then open localhost:8080
```

A dashboard built for the one thing Grafana can't show: **the agent
investigating, live**. Fleet heatmap (status or latency, log-scaled), a world
map of where each instance physically runs, z-score anomaly cards, and an SSE
stream of every tool call the agent makes on its way to an RCA. Every number
on it is fetched from Mimir/Loki/Tempo at request time — see
[`ARCHITECTURE.md` §18-19](ARCHITECTURE.md).

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

## The AIOps agent

An anomaly detector watches Mimir and hands anything unusual to a LangGraph
agent, which investigates across all three signals and writes the incident up.

```
detector (rolling z-score on PromQL)
        │  anomaly
        ▼
   recall ──▶ investigate ──▶ draft_rca ──▶ remember
   (past      (PromQL /        (structured   (episodic
   incidents)  LogQL /          verdict)      memory)
               TraceQL /
               SLO context)
```

```bash
pip install -r requirements.txt

python -m agent.main scan                    # one detector pass — no LLM calls at all
python -m agent.main investigate pg-local    # investigate on demand
python -m agent.main watch                   # detect and auto-investigate, continuously
python -m agent.main memory                  # what it has learned so far
python -m agent.main models                  # what your API key can reach
```

Or containerized, alongside the stack:

```bash
docker compose --profile agent up agent
```

### Cost

**Zero.** The agent runs on Google Gemini's free tier — put a key from
[aistudio.google.com/apikey](https://aistudio.google.com/apikey) in `.env` as
`GEMINI_API_KEY`. Argus is $0 end to end: infrastructure *and* inference.

Without a key the agent still runs the detector and logs what it finds; only
automatic root-cause analysis needs one. `python -m agent.main scan` is the
command to try first — it exercises the whole telemetry path and spends
nothing.

### Detection

A rolling z-score per instance per SLI: compare recent values against the
mean and standard deviation of the preceding window. This is a complement to
the SLO classification in `collector/`, not a replacement — the two catch
different failures:

| | fires when |
|---|---|
| SLO breach (`classify.py`) | a value crosses a fixed line we chose |
| z-score (`detector.py`) | a value is unlike *its own* recent history |

A target degrading *within* its budget trips only the detector. A dead cloud
instance is critical forever but isn't news, so it trips only the SLO.

It scores the recent **tail**, not just the newest sample — a 40-second spike
checked a minute later has already recovered, and scoring one point would
miss it entirely.

### Investigation

Four tools, one per question the agent needs answered:

| Tool | Backend | Answers |
|---|---|---|
| `query_metrics` | Mimir (PromQL) | what changed, and when |
| `query_logs` | Loki (LogQL) | why — the collector's own `status_reason` |
| `query_traces` | Tempo (TraceQL) | the individual poll, as a span |
| `get_slo_context` | `config.py` + `fleet.yaml` | what "bad" means for *this* instance |

`get_slo_context` reads the collector's real thresholds rather than a copy,
so the agent can't judge a value against numbers that have drifted from the
ones that produced it. It matters: several hosted targets run deliberately
widened budgets, so 300ms is fine on one instance and an incident on another.

The tool loop is hand-written rather than delegated to the SDK, so every call
is wrapped in an OpenTelemetry span. An investigation therefore shows up in
Tempo as a trace — `agent.investigation` with one `agent.tool.*` child per
query — sitting right next to the collector polls it was reasoning about. The
agent is observable by the stack it observes.

### Memory

Two tiers:

- **Working** — the turns and tool results of the current investigation, so
  the agent builds on its last query instead of restarting.
- **Episodic** — closed incidents on disk, recalled at the start of a later
  investigation and ranked instance > same-engine > unrelated.

Files and filtered recall, not a vector database. Recall here is filtered,
not fuzzy: the question is always "what happened to this instance, or this
engine, on this SLI, before?" — an exact-match query over a few labels. At
this volume that beats an embedding index and adds no extra service to run.

## What's next

Phases 1-4 are done — metrics, logs, traces, Kubernetes + ArgoCD, the AIOps
agent, and the live UI. See [`BUILD_PLAN.md`](BUILD_PLAN.md) for what each
phase decided and why, including the tradeoffs taken honestly (single-binary
Mimir/Loki/Tempo, no OTel Collector, no Alertmanager, files instead of
pgvector).

Three cloud targets currently have broken credentials (`ARCHITECTURE.md` §10)
— by design that costs nothing: `fleet.yaml` skips any target whose env vars
don't resolve, and the other eight keep reporting.
