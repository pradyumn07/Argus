"""
Fleet definition — builds the list of pollers from `deploy/fleet.yaml`.

Adding a target is a YAML edit, not a code change. This module only does the
wiring: resolve ${ENV_VAR} references, skip targets whose secrets aren't set,
resolve the per-target SLO, and instantiate the right poller class.

Secrets never live here or in the YAML — hosted targets reference ${ENV_VAR}
and resolve from the environment (.env locally, a k8s Secret on the cluster).
"""

import os
import re
from pathlib import Path

import yaml

from config import slo_for
from pollers.postgres import PostgresPoller
from pollers.mysql import MySQLPoller
from pollers.mongo import MongoPoller
from pollers.redis import RedisPoller

# Mounted at /etc/argus/fleet.yaml in the container; falls back to the repo
# copy so `cd collector && python main.py` still works outside Docker.
DEFAULT_FLEET_FILE = Path(__file__).resolve().parent.parent / "deploy" / "fleet.yaml"

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

_POLLERS = {
    "postgres": PostgresPoller,
    "mysql": MySQLPoller,
    "mongo": MongoPoller,
    "redis": RedisPoller,
}


class _Unresolved(Exception):
    """A ${VAR} in the DSN has no value — the target is skipped."""


def _interpolate(value: str) -> str:
    def sub(m: re.Match) -> str:
        env = os.getenv(m.group(1), "").strip()
        if not env:
            raise _Unresolved(m.group(1))
        return env
    return _ENV_REF.sub(sub, value)


def fleet_file() -> Path:
    return Path(os.getenv("ARGUS_FLEET_FILE", str(DEFAULT_FLEET_FILE)))


def load_targets() -> list[dict]:
    """Parse fleet.yaml into target dicts with defaults applied (no pollers)."""
    path = fleet_file()
    if not path.exists():
        raise SystemExit(f"Fleet file not found: {path}")
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defaults = doc.get("defaults") or {}
    targets = []
    for raw in doc.get("targets") or []:
        t = {**defaults, **raw}
        missing = [k for k in ("id", "engine", "dsn") if not t.get(k)]
        if missing:
            raise SystemExit(f"Fleet target {raw!r} is missing: {', '.join(missing)}")
        if t["engine"] not in _POLLERS:
            raise SystemExit(
                f"Fleet target {t['id']}: unknown engine {t['engine']!r} "
                f"(known: {', '.join(sorted(_POLLERS))})"
            )
        targets.append(t)
    return targets


def build_fleet():
    """Return [(meta_dict, poller), ...] for every resolvable target."""
    fleet = []
    for t in load_targets():
        try:
            dsn = _interpolate(str(t["dsn"]))
        except _Unresolved as exc:
            print(f"[fleet] skipping {t['id']}: ${{{exc}}} is not set")
            continue

        engine = t["engine"]
        cls = _POLLERS[engine]
        if engine == "mongo":
            poller = cls(t["id"], dsn, t.get("database") or "argus")
        else:
            poller = cls(t["id"], dsn)

        meta = {
            "id": t["id"],
            "engine": engine,
            "provider": t.get("provider", "docker"),
            "region": t.get("region", "local"),
            "poll_interval_s": t.get("poll_interval_s", 4),
            "slo": slo_for(engine, t.get("slo")),
        }
        fleet.append((meta, poller))
    return fleet
