"""Statistical anomaly detection over Mimir — rolling z-score.

This is the complement to `classify.py`, not a replacement:

  * classify.py answers "is this over the line we drew?" — a fixed SLO breach.
  * the detector answers "is this behaving unlike its own recent history?" —
    which fires on a target degrading *within* its budget, and stays quiet on
    a target that sits permanently over the line (a dead cloud instance is
    critical forever but is not news).

Deliberately a z-score and not an ML model. At one sample per few seconds
per instance there isn't enough data to justify anything heavier, and every
alert has to be explainable in one sentence — "latency is 6.2 standard
deviations above its own 30-minute mean" is defensible; a model score is not.
"""

from __future__ import annotations

import datetime
import statistics
import time
from dataclasses import dataclass

from . import clients, config


@dataclass
class Anomaly:
    instance_id: str
    engine: str
    metric: str
    z_score: float
    peak: float                  # the deviating value itself
    at_unix: float               # absolute time of the deviating sample
    seconds_ago: float           # age at detection time — rots, so never the only signal
    baseline_mean: float
    baseline_stddev: float
    recovered: bool

    def describe(self) -> str:
        z = "infinite" if self.z_score == float("inf") else f"{self.z_score:.1f}"
        state = "and has since recovered" if self.recovered else "and is ongoing"
        # ABSOLUTE time, not just "N seconds ago": a trigger is consumed later
        # than it is written — sometimes much later, if the agent is busy or
        # rate-limited — and a relative offset silently rots. An agent that
        # trusts a stale "91s ago" queries the wrong window and concludes the
        # signal never happened. This was a real wrong RCA.
        at = datetime.datetime.fromtimestamp(
            self.at_unix, datetime.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        return (
            f"{self.metric} for {self.instance_id} hit {self.peak:.3g} at {at} "
            f"({self.seconds_ago:.0f}s before this alert was raised; "
            f"{z} standard deviations from its {config.DETECT_WINDOW} baseline "
            f"of mean {self.baseline_mean:.3g}, stddev {self.baseline_stddev:.3g}) "
            f"{state}"
        )


def _z(value: float, mean: float, stddev: float) -> float | None:
    """z-score, with a guard for a perfectly flat baseline.

    A constant series has stddev 0, which makes any change infinitely many
    deviations away. Require the jump to also be materially large in absolute
    terms, so 0.0 -> 0.000001 on an idle metric isn't reported as infinite.
    """
    if stddev == 0:
        if value != mean and abs(value - mean) > max(abs(mean) * 0.5, 1e-6):
            return float("inf")
        return None
    return (value - mean) / stddev


def _z_scores(metric: str) -> list[Anomaly]:
    series = clients.promql_range(metric, window=config.DETECT_WINDOW, step=config.DETECT_STEP)
    now = time.time()
    found: list[Anomaly] = []

    for s in series:
        labels = s.get("labels", {})
        pairs = [(float(ts), float(v)) for ts, v in s.get("values", []) if v not in (None, "NaN")]
        tail_size = config.DETECT_RECENT_POINTS
        if len(pairs) < config.DETECT_MIN_SAMPLES + tail_size:
            continue

        baseline = [v for _, v in pairs[:-tail_size]]
        recent = pairs[-tail_size:]
        mean = statistics.fmean(baseline)
        stddev = statistics.pstdev(baseline)

        # Score every recent point and keep the most extreme. Scoring only the
        # newest one silently misses spikes that already recovered.
        worst: tuple[float, float, float] | None = None   # (z, timestamp, value)
        for ts, value in recent:
            z = _z(value, mean, stddev)
            if z is not None and (worst is None or abs(z) > abs(worst[0])):
                worst = (z, ts, value)

        if worst is None or abs(worst[0]) < config.DETECT_Z_THRESHOLD:
            continue

        worst_z, ts, peak = worst
        latest_value = recent[-1][1]
        latest_z = _z(latest_value, mean, stddev)
        found.append(Anomaly(
            instance_id=labels.get("instance_id", "?"),
            engine=labels.get("engine", "?"),
            metric=metric,
            z_score=worst_z,
            peak=peak,
            at_unix=ts,
            seconds_ago=max(0.0, now - ts),
            baseline_mean=mean,
            baseline_stddev=stddev,
            recovered=latest_z is None or abs(latest_z) < config.DETECT_Z_THRESHOLD,
        ))
    return found


def scan() -> list[Anomaly]:
    """One detection pass across every watched SLI. Worst z-score first."""
    out: list[Anomaly] = []
    for metric in config.WATCHED_SLIS:
        try:
            out.extend(_z_scores(metric))
        except clients.TelemetryError:
            continue  # one bad metric shouldn't abort the whole scan
    out.sort(key=lambda a: abs(a.z_score), reverse=True)
    return out


class Cooldown:
    """Suppresses repeat investigations of the same instance.

    Without this, a 40-second chaos run produces an investigation on every
    detector tick for as long as the spike lasts — same incident, billed
    and re-reasoned N times.
    """

    def __init__(self, seconds: int = config.DETECT_COOLDOWN_S):
        self.seconds = seconds
        self._last: dict[str, float] = {}

    def ready(self, instance_id: str) -> bool:
        last = self._last.get(instance_id)
        return last is None or (time.time() - last) >= self.seconds

    def mark(self, instance_id: str) -> None:
        self._last[instance_id] = time.time()
