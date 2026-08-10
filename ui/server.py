"""Argus UI — a small FastAPI backend behind the live dashboard.

It exists for three reasons the browser can't solve on its own:

  1. **Same-origin proxying.** Mimir, Loki, and Tempo don't send CORS headers,
     so a page fetching them directly is blocked. Everything goes through here.
  2. **Shaping.** Raw PromQL responses are matrices of strings; the heatmap
     wants a dense grid. Reshaping server-side keeps the client simple.
  3. **Streaming the agent.** An investigation takes 30-60s across several
     model calls. Rather than a spinner, it runs on a worker thread and pumps
     progress to the browser over SSE, so you watch it think.

Run:  python -m ui.server          (then open http://localhost:8080)
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

from agent import clients, config, detector  # noqa: E402
from agent.graph import AgentDeps, investigate, make_client  # noqa: E402
from agent.memory import EpisodicMemory  # noqa: E402
from agent.slo import locations, slo_context  # noqa: E402

log = logging.getLogger("argus.ui")
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Argus", docs_url=None, redoc_url=None)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


# ── Fleet snapshot ────────────────────────────────────────────────────

_SLI_QUERIES = {
    "status": "argus_status_level",
    "latency_ms": "argus_latency_ms",
    "err_rate": "argus_err_rate",
    "conn_pct": "argus_conn_pct",
    "ops_sec": "argus_ops_sec",
    "cache_hit_ratio": "argus_cache_hit_ratio",
    "storage_pct": "argus_storage_pct",
}


@app.get("/api/fleet")
def api_fleet() -> JSONResponse:
    """Current value of every SLI for every instance, keyed by instance_id."""
    fleet: dict[str, dict] = {}
    for field, query in _SLI_QUERIES.items():
        try:
            for row in clients.promql_instant(query):
                labels = row["labels"]
                iid = labels.get("instance_id")
                if not iid:
                    continue
                entry = fleet.setdefault(iid, {
                    "instance_id": iid,
                    "engine": labels.get("engine"),
                    "provider": labels.get("provider"),
                    "region": labels.get("region"),
                })
                try:
                    entry[field] = float(row["value"])
                except (TypeError, ValueError):
                    entry[field] = None
        except clients.TelemetryError as exc:
            log.warning("fleet query %s failed: %s", query, exc)

    # Geography for the world map, joined from fleet.yaml.
    geo = locations()
    for iid, entry in fleet.items():
        if iid in geo:
            entry["location"] = geo[iid]

    # An unreachable target still reports status=2 but has no latency at all —
    # surface that explicitly so the UI can say "unreachable" instead of "0ms".
    for entry in fleet.values():
        entry["unreachable"] = entry.get("status") == 2 and entry.get("latency_ms") is None

    return JSONResponse(sorted(fleet.values(), key=lambda e: e["instance_id"]))


# ── Heatmap ───────────────────────────────────────────────────────────

@app.get("/api/heatmap")
def api_heatmap(
    metric: str = Query("status", pattern="^(status|latency)$"),
    window: str = Query("30m"),
    step: str = Query("60s"),
) -> JSONResponse:
    """Instance x time grid.

    `status` is an ordered state (0/1/2) and gets the reserved status colors.
    `latency` is a continuous magnitude and gets a single-hue sequential ramp.
    They are different encodings of different things, so the client switches
    scale with the metric rather than recoloring one scale.
    """
    query = "argus_status_level" if metric == "status" else "argus_latency_ms"
    try:
        series = clients.promql_range(query, window=window, step=step)
    except clients.TelemetryError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)

    # Union of timestamps so every row shares one column axis; a target that
    # started late leaves real holes rather than shifting other rows.
    stamps: set[float] = set()
    for s in series:
        stamps.update(float(ts) for ts, _ in s["values"])
    times = sorted(stamps)
    index = {t: i for i, t in enumerate(times)}

    rows = []
    for s in series:
        labels = s["labels"]
        cells: list[float | None] = [None] * len(times)
        for ts, value in s["values"]:
            try:
                cells[index[float(ts)]] = float(value)
            except (KeyError, TypeError, ValueError):
                continue
        rows.append({
            "instance_id": labels.get("instance_id", "?"),
            "engine": labels.get("engine"),
            "provider": labels.get("provider"),
            "values": cells,
        })

    rows.sort(key=lambda r: (r["engine"] or "", r["instance_id"]))
    return JSONResponse({"metric": metric, "times": times, "rows": rows})


# ── Detector ──────────────────────────────────────────────────────────

@app.get("/api/anomalies")
def api_anomalies() -> JSONResponse:
    try:
        found = detector.scan()
    except Exception as exc:
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=502)
    return JSONResponse([{
        "instance_id": a.instance_id,
        "engine": a.engine,
        "metric": a.metric,
        "z_score": None if a.z_score in (float("inf"), float("-inf")) else round(a.z_score, 2),
        "infinite": a.z_score in (float("inf"), float("-inf")),
        "peak": a.peak,
        "at_unix": a.at_unix,
        "recovered": a.recovered,
        "description": a.describe(),
    } for a in found])


@app.get("/api/slo/{instance_id}")
def api_slo(instance_id: str) -> JSONResponse:
    return JSONResponse(slo_context(instance_id))


@app.get("/api/memory")
def api_memory() -> JSONResponse:
    return JSONResponse([{
        "id": i.id,
        "instance_id": i.instance_id,
        "engine": i.engine,
        "summary": i.summary,
        "root_cause": i.root_cause,
        "breached_slis": i.breached_slis,
        "recommendation": i.recommendation,
        "opened_at": i.opened_at,
    } for i in EpisodicMemory().all()])


# ── Streaming investigation ───────────────────────────────────────────

@app.get("/api/investigate")
def api_investigate(
    instance_id: str,
    trigger: str = "",
    engine: str = "",
) -> StreamingResponse:
    """Run one investigation, streaming progress as Server-Sent Events.

    The agent is synchronous, so it runs on a worker thread and posts progress
    into a queue that this generator drains. The alternative — waiting and
    returning the finished RCA — hides exactly the part worth watching.
    """
    events: queue.Queue = queue.Queue()

    def emit(event: dict) -> None:
        events.put(event)

    def run() -> None:
        try:
            client = make_client(required=False)
            if client is None:
                events.put({
                    "type": "error",
                    "message": "GEMINI_API_KEY is not set — detection works, "
                               "but root-cause analysis needs a key. "
                               "Get a free one at aistudio.google.com/apikey.",
                })
                return
            deps = AgentDeps(client=client, memory=EpisodicMemory(), on_event=emit)
            state = investigate(
                deps,
                instance_id,
                trigger or f"Manual investigation of {instance_id} requested from the UI. "
                           "Establish current health and explain anything abnormal.",
                engine=engine,
            )
            if state.get("error"):
                events.put({"type": "error", "message": state["error"]})
        except Exception as exc:
            events.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            events.put({"type": "done"})

    threading.Thread(target=run, daemon=True).start()

    def stream():
        yield _sse({"type": "started", "instance_id": instance_id})
        while True:
            try:
                event = events.get(timeout=30)
            except queue.Empty:
                yield ": keepalive\n\n"   # hold the connection through slow model calls
                continue
            yield _sse(event)
            if event.get("type") == "done":
                return

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


@app.get("/api/health")
def api_health() -> JSONResponse:
    """Which backends are reachable, and is the agent armed."""
    out = {"agent_key": bool(os.getenv(config.API_KEY_ENV, "").strip())}
    for name, probe in (
        ("mimir", lambda: clients.promql_instant("vector(1)")),
        ("loki", lambda: clients.logql_range('{job="argus"}', window="5m", limit=1)),
        ("tempo", lambda: clients.traceql_search(
            '{ resource.service.name = "argus-collector" }', window="15m", limit=1)),
    ):
        try:
            probe()
            out[name] = True
        except Exception:
            out[name] = False
    return JSONResponse(out)


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(message)s",
                        datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    port = int(os.getenv("ARGUS_UI_PORT", "8080"))
    print(f"\n  Argus UI  ->  http://localhost:{port}\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
