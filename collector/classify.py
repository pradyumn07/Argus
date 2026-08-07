"""Normalized metric sample + SLO classification."""

from dataclasses import dataclass, field

from config import SLO_BY_ENGINE, EngineSLO, SLI

_LEVELS = {0: "healthy", 1: "warning", 2: "critical"}
_RANK = {"healthy": 0, "warning": 1, "critical": 2}


@dataclass
class MetricSample:
    instance_id: str
    engine: str
    latency_ms: float | None = None
    conn_pct: float | None = None
    ops_sec: float | None = None
    err_rate: float | None = None
    cache_hit_ratio: float | None = None
    storage_pct: float | None = None
    status: str = "healthy"
    status_reason: str | None = None
    # set to True by a poller when the target could not be reached at all
    unreachable: bool = False
    _breaches: list[tuple[str, str]] = field(default_factory=list, repr=False)


def _level(value: float | None, sli: SLI) -> int:
    if value is None:
        return 0
    if sli.higher_is_worse:
        if value >= sli.crit:
            return 2
        if value >= sli.warn:
            return 1
    else:
        if value <= sli.crit:
            return 2
        if value <= sli.warn:
            return 1
    return 0


def classify(sample: MetricSample, slo: EngineSLO | None = None) -> MetricSample:
    """Evaluate every SLI against the SLO; status = worst breach.

    `slo` is the target's resolved SLO (engine defaults plus any per-target
    overrides from deploy/fleet.yaml). Falls back to the plain engine default
    so the function stays usable on its own.
    """
    if sample.unreachable:
        sample.status = "critical"
        # base.py already set a specific reason (e.g. "unreachable: OperationalError");
        # don't clobber it with a generic string if it's there.
        if not sample.status_reason:
            sample.status_reason = "unreachable"
        return sample

    slo = slo or SLO_BY_ENGINE[sample.engine]
    checks: list[tuple[str, float | None, SLI | None]] = [
        ("latency", sample.latency_ms, slo.latency_ms),
        ("connections", sample.conn_pct, slo.conn_pct),
        ("errors", sample.err_rate, slo.err_rate),
        ("storage", sample.storage_pct, slo.storage_pct),
        ("cache_hit", sample.cache_hit_ratio, slo.cache_hit_ratio),
    ]

    worst_level = 0
    worst_name = None
    for name, value, sli in checks:
        if sli is None or value is None:
            continue
        lvl = _level(value, sli)
        if lvl > worst_level:
            worst_level = lvl
            worst_name = name

    sample.status = _LEVELS[worst_level]
    if worst_level == 0:
        sample.status_reason = "within slo"
    else:
        # human-readable reason, e.g. "latency 4.1x over slo warn"
        sample.status_reason = _describe(worst_name, checks)
    return sample


def _describe(name, checks) -> str:
    for cname, value, sli in checks:
        if cname == name and value is not None and sli is not None:
            if sli.higher_is_worse:
                ref = sli.crit if value >= sli.crit else sli.warn
                factor = value / ref if ref else 0
                unit = "ms" if name == "latency" else ""
                return f"{name} {value:.1f}{unit} ({factor:.1f}x slo)"
            return f"{name} {value:.2f} below slo"
    return name


def worse_of(a: str, b: str) -> str:
    return a if _RANK[a] >= _RANK[b] else b