"""Exposes classified samples as Prometheus gauges on :9100/metrics.

A passive pull exporter: `record()` just updates in-memory gauges from
whatever each instance's poll loop last observed. Prometheus decides when
to scrape — this deliberately does not drive the poll cadence, so each
instance keeps polling on its own independent, jittered interval.
"""

from prometheus_client import Gauge, start_http_server

from classify import MetricSample

_LABELS = ("instance_id", "engine", "provider", "region")
_STATUS_LEVEL = {"healthy": 0, "warning": 1, "critical": 2}

latency_ms = Gauge("argus_latency_ms", "Round-trip latency", _LABELS)
conn_pct = Gauge("argus_conn_pct", "Connection saturation (0-1)", _LABELS)
ops_sec = Gauge("argus_ops_sec", "Throughput (ops/sec)", _LABELS)
err_rate = Gauge("argus_err_rate", "Error rate (0-1)", _LABELS)
cache_hit_ratio = Gauge("argus_cache_hit_ratio", "Cache hit ratio (0-1)", _LABELS)
storage_pct = Gauge("argus_storage_pct", "Storage used / soft cap (0-1)", _LABELS)
status_level = Gauge(
    "argus_status_level", "SLO status: 0=healthy 1=warning 2=critical", _LABELS
)


def start(port: int = 9100) -> None:
    start_http_server(port)


def record(sample: MetricSample, meta: dict) -> None:
    labels = {
        "instance_id": sample.instance_id,
        "engine": sample.engine,
        "provider": meta["provider"],
        "region": meta["region"],
    }
    for gauge, value in (
        (latency_ms, sample.latency_ms),
        (conn_pct, sample.conn_pct),
        (ops_sec, sample.ops_sec),
        (err_rate, sample.err_rate),
        (cache_hit_ratio, sample.cache_hit_ratio),
        (storage_pct, sample.storage_pct),
    ):
        if value is not None:
            gauge.labels(**labels).set(value)
    status_level.labels(**labels).set(_STATUS_LEVEL[sample.status])
