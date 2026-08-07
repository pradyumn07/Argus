"""
SLO thresholds — the single source of truth for what 'healthy' means.

Each SLI has (warn, crit) cut-offs. Two directions:
  - HIGHER-is-worse SLIs (latency, conn_pct, err_rate, storage_pct):
        value >= crit  -> critical
        value >= warn  -> warning
  - LOWER-is-worse SLIs (cache_hit_ratio):
        value <= crit  -> critical
        value <= warn  -> warning

Rationale for the latency budgets (interview-defensible):
  - Redis is an in-memory store fronting hot reads, so a p99 above ~10ms
    already signals trouble (network + serialization dominate).
  - Postgres here backs OLTP-style traffic; ~50ms p99 is a reasonable SLO
    for simple indexed queries on a free tier.
  - MySQL runs the same OLTP-style workload as Postgres, so it shares that
    ~50ms budget — the engine differs, the operational expectation doesn't.
  - Mongo document reads sit between the two (~40ms).

conn_pct, err_rate, storage_pct budgets are shared across engines because
they express the same operational risk (saturation, failure, capacity).

These are the LOCAL budgets. A hosted, cross-region free-tier instance can't
meet a localhost latency budget, so individual targets widen theirs via the
`slo:` block in deploy/fleet.yaml — see slo_for() below.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SLI:
    warn: float
    crit: float
    higher_is_worse: bool = True


@dataclass(frozen=True)
class EngineSLO:
    latency_ms: SLI
    conn_pct: SLI
    err_rate: SLI
    storage_pct: SLI
    cache_hit_ratio: SLI | None = None  # not all targets expose this


_SATURATION = SLI(warn=0.70, crit=0.90)              # 70% warn, 90% crit
_ERRORS = SLI(warn=0.01, crit=0.03)                  # 1% warn, 3% crit
_CAPACITY = SLI(warn=0.75, crit=0.90)                # 75% warn, 90% crit
_CACHE = SLI(warn=0.95, crit=0.85, higher_is_worse=False)  # good caches stay >95%

SLO_BY_ENGINE: dict[str, EngineSLO] = {
    "postgres": EngineSLO(
        latency_ms=SLI(warn=50, crit=150),
        conn_pct=_SATURATION,
        err_rate=_ERRORS,
        storage_pct=_CAPACITY,
        cache_hit_ratio=_CACHE,
    ),
    "mysql": EngineSLO(
        latency_ms=SLI(warn=50, crit=150),
        conn_pct=_SATURATION,
        err_rate=_ERRORS,
        storage_pct=_CAPACITY,
        cache_hit_ratio=_CACHE,      # InnoDB buffer pool hit ratio
    ),
    "mongo": EngineSLO(
        latency_ms=SLI(warn=40, crit=120),
        conn_pct=_SATURATION,
        err_rate=_ERRORS,
        storage_pct=_CAPACITY,
        cache_hit_ratio=None,
    ),
    "redis": EngineSLO(
        latency_ms=SLI(warn=10, crit=30),
        conn_pct=_SATURATION,
        err_rate=_ERRORS,
        storage_pct=_CAPACITY,
        cache_hit_ratio=None,
    ),
}

# Soft storage caps (bytes) used to compute storage_pct. Free-tier ceilings.
STORAGE_CAP_BYTES = {
    "postgres": 512 * 1024 * 1024,   # ~0.5 GB (Neon/Supabase free)
    "mysql": 512 * 1024 * 1024,      # same class of free-tier ceiling as Postgres
    "mongo": 512 * 1024 * 1024,      # M0 = 512 MB
    "redis": 256 * 1024 * 1024,      # Upstash free soft cap; overridden by maxmemory if present
}

_SLI_FIELDS = ("latency_ms", "conn_pct", "err_rate", "storage_pct", "cache_hit_ratio")


def slo_for(engine: str, overrides: dict | None = None) -> EngineSLO:
    """Engine SLO with optional per-target overrides from deploy/fleet.yaml.

    Overrides exist because one budget can't fit both a container on the
    compose network and a cross-region free-tier instance. Direction
    (higher_is_worse) is never overridable — that's a property of the SLI
    itself, not of where the target happens to live.
    """
    base = SLO_BY_ENGINE[engine]
    if not overrides:
        return base

    unknown = set(overrides) - set(_SLI_FIELDS)
    if unknown:
        raise SystemExit(
            f"Unknown SLO override(s) for {engine}: {', '.join(sorted(unknown))}"
        )

    resolved = {}
    for name in _SLI_FIELDS:
        current = getattr(base, name)
        override = overrides.get(name)
        if override is None:
            resolved[name] = current
            continue
        if current is None:
            # Engine doesn't define this SLI at all (e.g. mongo cache_hit_ratio);
            # an override can't invent one, since nothing collects it.
            raise SystemExit(f"{engine} does not report {name} — cannot override it")
        resolved[name] = SLI(
            warn=float(override.get("warn", current.warn)),
            crit=float(override.get("crit", current.crit)),
            higher_is_worse=current.higher_is_worse,
        )
    return EngineSLO(**resolved)
