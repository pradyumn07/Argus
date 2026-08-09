"""Grounding: what "healthy" actually means for a given instance.

Reads the SAME sources the collector classifies against — engine defaults
from collector/config.py and per-target overrides from deploy/fleet.yaml —
so the agent can never reason against thresholds that have drifted from the
ones producing the metrics. Duplicating the numbers here would guarantee
that drift eventually.

Only collector/config.py is imported (pure dataclasses, no drivers);
fleet.yaml is parsed directly rather than going through collector/fleet.py,
which pulls in asyncpg/motor/redis and has no business in this image.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "collector"))

import config as collector_config  # noqa: E402

FLEET_FILE = Path(os.getenv("ARGUS_FLEET_FILE", str(_REPO / "deploy" / "fleet.yaml")))

# How each SLI is actually derived, per engine — the agent needs this to
# interpret a number, not just compare it. Mirrors the poller docstrings.
_HOW_MEASURED = {
    "postgres": {
        "latency_ms": "round-trip of SELECT 1",
        "conn_pct": "active backends / max_connections (pg_stat_activity)",
        "err_rate": "rollbacks / (commits + rollbacks)",
        "cache_hit_ratio": "blks_hit / (blks_hit + blks_read)",
        "storage_pct": "pg_database_size / soft cap",
    },
    "mysql": {
        "latency_ms": "round-trip of SELECT 1",
        "conn_pct": "Threads_connected / max_connections",
        "err_rate": "delta Aborted_connects / delta Connections",
        "cache_hit_ratio": "InnoDB buffer pool hit ratio",
        "storage_pct": "SUM(data_length + index_length) / soft cap",
    },
    "mongo": {
        "latency_ms": "round-trip of ping",
        "conn_pct": "connections.current / (current + available)",
        "err_rate": "delta metrics.commands[*].failed / delta [*].total",
        "cache_hit_ratio": "not reported by this engine",
        "storage_pct": "dbStats().dataSize / soft cap",
    },
    "redis": {
        "latency_ms": "round-trip of PING",
        "conn_pct": "connected_clients / maxclients",
        "err_rate": "delta rejected_connections / delta total_connections",
        "cache_hit_ratio": "keyspace_hits / (hits + misses)",
        "storage_pct": "used_memory / maxmemory",
    },
}

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _targets() -> dict[str, dict]:
    doc = yaml.safe_load(FLEET_FILE.read_text(encoding="utf-8")) or {}
    defaults = doc.get("defaults") or {}
    return {t["id"]: {**defaults, **t} for t in (doc.get("targets") or [])}


def _sli(sli) -> dict | None:
    if sli is None:
        return None
    return {
        "warn": sli.warn,
        "crit": sli.crit,
        "direction": "higher is worse" if sli.higher_is_worse else "lower is worse",
    }


def slo_context(instance_id: str) -> dict:
    targets = _targets()
    target = targets.get(instance_id)
    if target is None:
        return {
            "error": f"unknown instance {instance_id!r}",
            "known_instances": sorted(targets),
        }

    engine = target["engine"]
    overrides = target.get("slo") or {}
    slo = collector_config.slo_for(engine, overrides)

    return {
        "instance_id": instance_id,
        "engine": engine,
        "provider": target.get("provider"),
        "region": target.get("region"),
        "poll_interval_s": target.get("poll_interval_s"),
        "thresholds": {
            "latency_ms": _sli(slo.latency_ms),
            "conn_pct": _sli(slo.conn_pct),
            "err_rate": _sli(slo.err_rate),
            "storage_pct": _sli(slo.storage_pct),
            "cache_hit_ratio": _sli(slo.cache_hit_ratio),
        },
        "overridden_slis": sorted(overrides) or None,
        "how_measured": _HOW_MEASURED.get(engine, {}),
        "status_levels": {"0": "healthy", "1": "warning", "2": "critical"},
        "note": (
            "status is the WORST breaching SLI, so a critical instance may be "
            "fine on every SLI except one. Overridden SLIs are widened on "
            "purpose for cross-region hosted targets — judge against these "
            "numbers, not against the engine defaults."
        ),
    }
