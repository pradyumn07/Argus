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
  - Mongo document reads sit between the two (~40ms).

conn_pct, err_rate, storage_pct budgets are shared across engines because
they express the same operational risk (saturation, failure, capacity).
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
    "mongo": 512 * 1024 * 1024,      # M0 = 512 MB
    "redis": 256 * 1024 * 1024,      # Upstash free soft cap; overridden by maxmemory if present
}
