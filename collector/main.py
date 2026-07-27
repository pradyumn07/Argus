"""
Argus collector — the always-on heartbeat of the fleet.

Runs one independent asyncio task per instance. Each task polls on its own
interval (with jitter), classifies the sample against the engine SLO, and
writes it to the metrics store. Independent intervals are deliberate: a real
fleet doesn't blink in unison, and the frontend's pulse animation keys off
exactly when each poll lands.

Run:
    cd collector
    python main.py
"""

import asyncio
import os
import random
import signal

from dotenv import load_dotenv

from classify import classify
from fleet import build_fleet
from store import MetricStore

load_dotenv()

_stop = asyncio.Event()


async def poll_loop(meta, poller, store: MetricStore):
    interval = float(meta["poll_interval_s"])
    # stagger startup so tasks don't all fire at t=0
    await asyncio.sleep(random.uniform(0, interval))
    while not _stop.is_set():
        sample = await poller.poll()
        sample = classify(sample)
        try:
            await store.write(sample)
        except Exception as exc:
            print(f"[store] write failed for {meta['id']}: {exc}")
        lat = f"{sample.latency_ms:.1f}ms" if sample.latency_ms is not None else "  n/a"
        print(f"[{meta['id']:<14}] {sample.status:<8} {lat:<8} {sample.status_reason}")
        # jittered sleep keeps the fleet visually alive
        await asyncio.sleep(interval + random.uniform(-0.3, 0.6))


async def main():
    store_dsn = os.environ.get("STORE_DSN")
    if not store_dsn:
        raise SystemExit("Set STORE_DSN (your Supabase Postgres connection string).")

    store = MetricStore(store_dsn)
    await store.connect()

    fleet = build_fleet()
    if not fleet:
        raise SystemExit("No targets configured. Set at least one *_DSN / *_URL env var.")

    print(f"Argus collector online — {len(fleet)} target(s): "
          + ", ".join(m['id'] for m, _ in fleet))

    tasks = [asyncio.create_task(poll_loop(m, p, store)) for m, p in fleet]

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop.set)
        except NotImplementedError:
            pass  # Windows

    await _stop.wait()
    print("\nShutting down…")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for _, p in fleet:
        await p.close()
    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
