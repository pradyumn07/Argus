"""
Argus collector — the always-on heartbeat of the fleet.

Runs one independent asyncio task per instance. Each task polls on its own
interval (with jitter), classifies the sample against the engine SLO, and
updates the Prometheus exporter. Independent intervals are deliberate: a
real fleet doesn't blink in unison, and Prometheus scrapes on its own
separate cadence regardless of when each instance's poll lands.

Each poll emits all three signals for the same event: a metric (exporter),
a structured log line (logs.py), and a trace span (tracing.py) — correlated
by trace_id so Grafana can pivot between them.

Run:
    cd collector
    python main.py
"""

import asyncio
import os
import random
import signal

from dotenv import load_dotenv

import exporter
import logs
import tracing
from classify import classify
from fleet import build_fleet

load_dotenv()

_stop = asyncio.Event()
log = logs.setup()


async def poll_loop(meta, poller):
    interval = float(meta["poll_interval_s"])
    # stagger startup so tasks don't all fire at t=0
    await asyncio.sleep(random.uniform(0, interval))
    while not _stop.is_set():
        # The span wraps poll + classify + record, and the log line is
        # emitted inside it so the record carries this span's trace_id.
        with tracing.tracer().start_as_current_span("argus.poll") as span:
            span.set_attribute("argus.instance_id", meta["id"])
            span.set_attribute("argus.provider", meta["provider"])
            span.set_attribute("argus.region", meta["region"])
            span.set_attribute("db.system", meta["engine"])

            sample = await poller.poll()
            sample = classify(sample, meta.get("slo"))
            exporter.record(sample, meta)

            span.set_attribute("argus.status", sample.status)
            span.set_attribute("argus.unreachable", sample.unreachable)
            if sample.status_reason:
                span.set_attribute("argus.status_reason", sample.status_reason)
            if sample.latency_ms is not None:
                span.set_attribute("argus.latency_ms", round(sample.latency_ms, 3))

            log.info(
                "poll",
                extra={
                    "instance_id": meta["id"],
                    "engine": meta["engine"],
                    "provider": meta["provider"],
                    "region": meta["region"],
                    "status": sample.status,
                    "status_reason": sample.status_reason,
                    "latency_ms": (
                        round(sample.latency_ms, 3)
                        if sample.latency_ms is not None else None
                    ),
                    "unreachable": sample.unreachable,
                },
            )
        # jittered sleep keeps the fleet visually alive
        await asyncio.sleep(interval + random.uniform(-0.3, 0.6))


async def main():
    exporter_port = int(os.environ.get("EXPORTER_PORT", "9100"))
    exporter.start(exporter_port)
    otlp_endpoint = tracing.start()

    fleet = build_fleet()
    if not fleet:
        raise SystemExit("No targets configured. Set at least one *_DSN / *_URL env var.")

    log.info(
        "collector.online",
        extra={
            "targets": len(fleet),
            "instance_ids": [m["id"] for m, _ in fleet],
            "metrics_port": exporter_port,
            "tracing": otlp_endpoint or "disabled",
        },
    )

    tasks = [asyncio.create_task(poll_loop(m, p)) for m, p in fleet]

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop.set)
        except NotImplementedError:
            pass  # Windows

    await _stop.wait()
    log.info("collector.shutdown")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for _, p in fleet:
        await p.close()


if __name__ == "__main__":
    asyncio.run(main())
