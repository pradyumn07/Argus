"""The agent's tool surface: PromQL, LogQL, TraceQL, and SLO context.

Four tools, not forty. Each maps to one telemetry backend plus one grounding
tool, because the model picks tools from their descriptions and a crowded
surface makes that choice worse, not better.

Descriptions state *when* to call each tool, not just what it does — that's
what actually drives correct tool selection.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from google.genai import types

from . import clients
from .slo import slo_context

# Results are truncated before they reach the model: a range query over 11
# instances is thousands of points, and pasting all of it in crowds out the
# reasoning it's meant to support.
MAX_SERIES = 12
MAX_POINTS = 60


def _tool(name: str, description: str, properties: dict, required: list[str]) -> types.FunctionDeclaration:
    return types.FunctionDeclaration(
        name=name,
        description=description,
        parameters_json_schema={
            "type": "object",
            "properties": properties,
            "required": required,
        },
    )


DECLARATIONS = [
    _tool(
        "query_metrics",
        "Run a PromQL query against Mimir. Call this first for any question about "
        "what changed and when — latency, error rate, connection saturation, "
        "throughput, storage, or SLO status over time. Available metrics: "
        "argus_latency_ms, argus_err_rate, argus_conn_pct, argus_ops_sec, "
        "argus_cache_hit_ratio, argus_storage_pct, argus_status_level "
        "(0=healthy 1=warning 2=critical). Every metric is labelled with "
        "instance_id, engine, provider, region.",
        {
            "query": {"type": "string", "description": "PromQL expression."},
            "range": {
                "type": "boolean",
                "description": "True for a time series over a window (use this to see a "
                               "change over time); false for current values only.",
            },
            "window": {
                "type": "string",
                "description": "Lookback ending NOW — a plain duration only, e.g. 15m, "
                               "2h, 1d. To inspect a past moment, keep this a plain "
                               "duration wide enough to reach back to it, or anchor "
                               "inside the query with PromQL's @ modifier, e.g. "
                               "max_over_time(metric[5m] @ 1786369318).",
            },
        },
        ["query"],
    ),
    _tool(
        "query_logs",
        "Run a LogQL query against Loki. Call this once metrics show *that* something "
        "changed, to find out *why* — the collector logs one JSON object per poll with "
        "a human-readable status_reason and the exception name for unreachable targets. "
        'Labels: job="argus", instance_id, engine, status (healthy|warning|critical), '
        'level. Example: {job="argus", instance_id="pg-local", status="critical"} | json',
        {
            "query": {"type": "string", "description": "LogQL expression."},
            "window": {"type": "string", "description": "Lookback, e.g. 15m, 2h."},
            "limit": {"type": "integer", "description": "Max lines (default 30, cap 100)."},
        },
        ["query"],
    ),
    _tool(
        "query_traces",
        "Search or fetch OpenTelemetry traces from Tempo. Each collector poll is one "
        "'argus.poll' span carrying argus.instance_id, argus.status, argus.latency_ms, "
        "argus.status_reason, db.system. Call this to inspect individual slow or failed "
        "polls after logs have narrowed the window. Pass trace_id to fetch one trace, or "
        'query for a TraceQL search like { span.argus.status = "critical" }.',
        {
            "query": {"type": "string", "description": "TraceQL search expression."},
            "trace_id": {"type": "string", "description": "Fetch this exact trace instead of searching."},
            "window": {"type": "string", "description": "Lookback for searches, e.g. 15m."},
        },
        [],
    ),
    _tool(
        "get_slo_context",
        "Return the SLO thresholds that define healthy/warning/critical for an instance, "
        "plus how each SLI is actually measured for that engine. Call this before "
        "concluding a value is bad — thresholds are engine-specific and several hosted "
        "targets deliberately run widened budgets, so a raw number means nothing without "
        "the budget it is judged against.",
        {"instance_id": {"type": "string", "description": "e.g. pg-local, mongo-orders."}},
        ["instance_id"],
    ),
]

TOOLS = [types.Tool(function_declarations=DECLARATIONS)]


# ── Dispatch ──────────────────────────────────────────────────────────

def _downsample(points: list, limit: int = MAX_POINTS) -> tuple[list, bool]:
    """Thin a series to `limit` points spread across the WHOLE window.

    Emphatically not "keep the newest N": that silently shrinks the window the
    caller asked for. It produced a real wrong answer — the agent widened its
    query to 6h to find a spike, truncation cut the result back to the most
    recent ~30 minutes, and the agent concluded the spike never happened.

    Even sampling keeps the shape and the extremes of the requested range, so
    widening the window actually widens what the model sees.
    """
    if len(points) <= limit:
        return points, False
    step = (len(points) - 1) / (limit - 1)
    idx = sorted({round(i * step) for i in range(limit)} | {0, len(points) - 1})
    return [points[i] for i in idx], True


def _query_metrics(query: str, range: bool = False, window: str = "30m") -> Any:
    if range:
        series = clients.promql_range(query, window=window)[:MAX_SERIES]
        for s in series:
            pts = s["values"]
            kept, thinned = _downsample(pts)
            s["values"] = kept
            if thinned:
                # Say so explicitly — a thinned series can hide a spike
                # narrower than the sampling interval.
                s["note"] = (
                    f"downsampled evenly from {len(pts)} to {len(kept)} points across "
                    f"the full {window} window; a spike shorter than "
                    f"~{len(pts) // len(kept)} samples may not appear. Narrow the "
                    f"window for full resolution, or use max_over_time()."
                )
        return series
    return clients.promql_instant(query)[:MAX_SERIES]


def _query_logs(query: str, window: str = "30m", limit: int = 30) -> Any:
    return clients.logql_range(query, window=window, limit=min(int(limit), 100))


def _query_traces(query: str = "", trace_id: str = "", window: str = "30m") -> Any:
    if trace_id:
        return clients.trace_by_id(trace_id)
    if not query:
        raise clients.TelemetryError("pass either query or trace_id")
    return clients.traceql_search(query, window=window)


_HANDLERS: dict[str, Callable[..., Any]] = {
    "query_metrics": _query_metrics,
    "query_logs": _query_logs,
    "query_traces": _query_traces,
    "get_slo_context": slo_context,
}


def call(name: str, args: dict) -> dict:
    """Execute one tool call. Never raises — errors go back to the model.

    A failed tool is information the model can act on (fix the query, try a
    different backend); an exception here would end the investigation.
    """
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        return {"result": handler(**(args or {}))}
    except TypeError as exc:
        return {"error": f"bad arguments for {name}: {exc}"}
    except clients.TelemetryError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # never kill the investigation
        return {"error": f"{type(exc).__name__}: {exc}"}


def preview(payload: dict, limit: int = 220) -> str:
    """Compact one-line rendering of a tool result, for logs."""
    text = json.dumps(payload, default=str)
    return text if len(text) <= limit else text[:limit] + "…"
