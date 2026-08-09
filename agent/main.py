"""Argus AIOps agent — CLI.

    python -m agent.main models                     # what your key can reach
    python -m agent.main scan                       # one detector pass, no model calls
    python -m agent.main investigate pg-local       # investigate on demand
    python -m agent.main watch                      # detect -> investigate, continuously
    python -m agent.main memory                     # show episodic memory

`scan` and `models` are the two commands worth running first: `scan` proves
the telemetry path works without spending a single token, and `models`
proves the key works before an investigation fails halfway through.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

from dotenv import load_dotenv

from . import config, detector, tracing
from .graph import NO_KEY_MESSAGE, AgentDeps, investigate, make_client
from .memory import EpisodicMemory

load_dotenv()
log = logging.getLogger("argus.agent")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # These log a line per HTTP call; the agent makes many per investigation
    # and their noise buries the tool trace that actually matters.
    for noisy in ("httpx", "httpcore", "google_genai", "urllib3", "opentelemetry"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _deps() -> AgentDeps:
    return AgentDeps(client=make_client(), memory=EpisodicMemory())


def _print_rca(state: dict) -> None:
    if state.get("error"):
        print(f"\n  INVESTIGATION FAILED: {state['error']}")
        if state.get("rca", {}).get("raw"):
            print(f"  raw model output: {state['rca']['raw'][:500]}")
        return
    rca = state.get("rca") or {}
    print("\n" + "=" * 72)
    print(f"  {rca.get('summary', '(no summary)')}")
    print("=" * 72)
    print(f"\n  ROOT CAUSE ({rca.get('confidence', 'unknown')} confidence)")
    print(f"    {rca.get('root_cause', '-')}")
    if rca.get("breached_slis"):
        print(f"\n  BREACHED   {', '.join(rca['breached_slis'])}")
    if rca.get("evidence"):
        print("\n  EVIDENCE")
        for e in rca["evidence"]:
            print(f"    - {e}")
    print(f"\n  RECOMMENDATION\n    {rca.get('recommendation', '-')}")
    print(f"\n  ({state.get('tool_calls', 0)} tool calls; incident "
          f"{state.get('incident_id', 'not saved')})\n")


def cmd_models(_args) -> None:
    client = make_client()
    print("Models your key can reach (look for a free-tier flash model):\n")
    for m in client.models.list():
        actions = getattr(m, "supported_actions", None) or []
        if not actions or "generateContent" in actions:
            print(f"  {m.name}")
    print(f"\nCurrently configured: ARGUS_AGENT_MODEL={config.MODEL}")


def cmd_scan(_args) -> None:
    found = detector.scan()
    if not found:
        print(f"No anomalies (|z| >= {config.DETECT_Z_THRESHOLD} over {config.DETECT_WINDOW}).")
        return
    print(f"{len(found)} anomaly/anomalies:\n")
    for a in found:
        print(f"  {a.describe()}")


def cmd_investigate(args) -> None:
    trigger = args.trigger or (
        f"Manual investigation requested for {args.instance_id}. "
        "Establish current health and explain anything abnormal."
    )
    state = investigate(_deps(), args.instance_id, trigger, engine=args.engine or "")
    _print_rca(state)


def cmd_watch(args) -> None:
    # Detection needs no model. Run it either way and light up RCA only when
    # a key is present, so a missing credential degrades the service instead
    # of crash-looping it.
    client = make_client(required=False)
    deps = AgentDeps(client=client, memory=EpisodicMemory()) if client else None
    cooldown = detector.Cooldown()

    log.info(
        "watching every %ds (|z| >= %.1f over %s, cooldown %ds)",
        config.DETECT_INTERVAL_S, config.DETECT_Z_THRESHOLD,
        config.DETECT_WINDOW, config.DETECT_COOLDOWN_S,
    )
    if deps is None:
        log.warning("DETECT-ONLY MODE: %s", NO_KEY_MESSAGE)
        log.warning("anomalies will be logged; automatic RCA is disabled")

    while True:
        try:
            for anomaly in detector.scan():
                if not cooldown.ready(anomaly.instance_id):
                    continue
                cooldown.mark(anomaly.instance_id)
                log.info("ANOMALY %s", anomaly.describe())
                if deps is None:
                    continue
                state = investigate(
                    deps, anomaly.instance_id, anomaly.describe(), engine=anomaly.engine
                )
                _print_rca(state)
                if args.once:
                    return
        except KeyboardInterrupt:
            log.info("stopping")
            return
        except Exception as exc:  # a bad tick must not end the watch
            log.error("scan failed: %s: %s", type(exc).__name__, exc)
        time.sleep(config.DETECT_INTERVAL_S)


def cmd_memory(_args) -> None:
    incidents = EpisodicMemory().all()
    if not incidents:
        print(f"No incidents recorded in {config.MEMORY_DIR}.")
        return
    print(f"{len(incidents)} incident(s), newest first:\n")
    for inc in incidents:
        print(f"  {inc.headline()}")


def main() -> None:
    _setup_logging()
    tracing.start()

    p = argparse.ArgumentParser(prog="agent", description="Argus AIOps agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("models", help="list models your API key can reach")
    sub.add_parser("scan", help="one detector pass (no model calls)")
    sub.add_parser("memory", help="show episodic memory")

    i = sub.add_parser("investigate", help="investigate one instance now")
    i.add_argument("instance_id")
    i.add_argument("--trigger", help="what prompted this")
    i.add_argument("--engine", help="postgres|mysql|mongo|redis")

    w = sub.add_parser("watch", help="detect anomalies and auto-investigate")
    w.add_argument("--once", action="store_true", help="exit after the first investigation")

    args = p.parse_args()
    {
        "models": cmd_models,
        "scan": cmd_scan,
        "investigate": cmd_investigate,
        "watch": cmd_watch,
        "memory": cmd_memory,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
