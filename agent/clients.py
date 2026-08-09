"""Thin read-only HTTP clients for Mimir, Loki, and Tempo.

Deliberately read-only: the agent investigates, it never mutates telemetry.
Every method returns plain Python data, already trimmed — what comes back
goes straight into the model's context, so an unbounded raw response is a
context-window problem, not just a bandwidth one.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from . import config

_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


class TelemetryError(RuntimeError):
    """A backend rejected the query. Surfaced to the model as a tool error."""


def _get(url: str, params: dict[str, Any]) -> dict:
    try:
        r = httpx.get(url, params=params, timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        raise TelemetryError(f"{type(exc).__name__}: {exc}") from exc
    if r.status_code >= 400:
        raise TelemetryError(f"HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


# ── Mimir (PromQL) ────────────────────────────────────────────────────

def promql_instant(query: str) -> list[dict]:
    """Current value per series."""
    data = _get(f"{config.MIMIR_URL}/prometheus/api/v1/query", {"query": query})
    out = []
    for series in data.get("data", {}).get("result", []):
        value = series.get("value") or [None, None]
        out.append({"labels": series.get("metric", {}), "value": value[1]})
    return out


def promql_range(query: str, window: str = "30m", step: str = "30s") -> list[dict]:
    """Time series over a window. Values are [unix_seconds, value] pairs."""
    now = int(time.time())
    data = _get(
        f"{config.MIMIR_URL}/prometheus/api/v1/query_range",
        {"query": query, "start": now - _duration_s(window), "end": now, "step": step},
    )
    return [
        {"labels": s.get("metric", {}), "values": s.get("values", [])}
        for s in data.get("data", {}).get("result", [])
    ]


# ── Loki (LogQL) ──────────────────────────────────────────────────────

def logql_range(query: str, window: str = "30m", limit: int = 50) -> list[dict]:
    """Log lines newest-first. `limit` is capped by the caller, not Loki."""
    now_ns = int(time.time() * 1_000_000_000)
    data = _get(
        f"{config.LOKI_URL}/loki/api/v1/query_range",
        {
            "query": query,
            "start": now_ns - _duration_s(window) * 1_000_000_000,
            "end": now_ns,
            "limit": limit,
            "direction": "backward",
        },
    )
    lines: list[dict] = []
    for stream in data.get("data", {}).get("result", []):
        for ts_ns, line in stream.get("values", []):
            lines.append({"ts": int(ts_ns) / 1e9, "labels": stream.get("stream", {}), "line": line})
    lines.sort(key=lambda x: x["ts"], reverse=True)
    return lines[:limit]


# ── Tempo (TraceQL) ───────────────────────────────────────────────────

def traceql_search(query: str, window: str = "30m", limit: int = 10) -> list[dict]:
    now = int(time.time())
    data = _get(
        f"{config.TEMPO_URL}/api/search",
        {"q": query, "start": now - _duration_s(window), "end": now, "limit": limit},
    )
    return [
        {
            "trace_id": t.get("traceID"),
            "root_service": t.get("rootServiceName"),
            "root_name": t.get("rootTraceName"),
            "duration_ms": t.get("durationMs"),
        }
        for t in (data.get("traces") or [])
    ]


def trace_by_id(trace_id: str) -> dict:
    """One trace, flattened to spans with their attributes."""
    data = _get(f"{config.TEMPO_URL}/api/traces/{trace_id}", {})
    spans: list[dict] = []
    for batch in data.get("batches", []):
        resource = {
            a["key"]: _attr_value(a["value"])
            for a in batch.get("resource", {}).get("attributes", [])
        }
        for scope in batch.get("scopeSpans", []):
            for span in scope.get("spans", []):
                spans.append({
                    "name": span.get("name"),
                    "service": resource.get("service.name"),
                    "attributes": {
                        a["key"]: _attr_value(a["value"])
                        for a in span.get("attributes", [])
                    },
                })
    return {"trace_id": trace_id, "spans": spans}


def _attr_value(v: dict) -> Any:
    """OTLP wraps every attribute value in a one-key type envelope."""
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in v:
            return v[key]
    return next(iter(v.values()), None)


def _duration_s(text: str) -> int:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    text = text.strip()
    if text and text[-1] in units and text[:-1].isdigit():
        return int(text[:-1]) * units[text[-1]]
    if text.isdigit():
        return int(text)
    raise TelemetryError(f"bad duration {text!r} — use forms like 15m, 2h, 1d")
