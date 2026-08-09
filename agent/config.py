"""Agent configuration — endpoints, model, and investigation limits.

Everything is env-overridable so the same image runs against docker-compose
service names, Kubernetes Service DNS, or localhost port-forwards.
"""

import os

# ── Telemetry backends ────────────────────────────────────────────────
# Same DNS names the collector and Grafana already use (see deploy/).
MIMIR_URL = os.getenv("MIMIR_URL", "http://mimir:9009")
LOKI_URL = os.getenv("LOKI_URL", "http://loki:3100")
TEMPO_URL = os.getenv("TEMPO_URL", "http://tempo:3200")

# ── Model ─────────────────────────────────────────────────────────────
# Google Gemini, chosen for its genuinely free API tier — that keeps Argus
# at $0 end to end, infrastructure and inference alike.
#
# Verify what your key can actually reach with `python -m agent.main models`
# before assuming this default exists on your account; free-tier model
# availability changes and differs per key.
MODEL = os.getenv("ARGUS_AGENT_MODEL", "gemini-2.5-flash")
API_KEY_ENV = "GEMINI_API_KEY"

MAX_OUTPUT_TOKENS = int(os.getenv("ARGUS_AGENT_MAX_TOKENS", "8192"))

# Hard ceiling on tool-calling rounds per investigation. Without this a
# confused agent loops on queries indefinitely and burns the free-tier quota.
MAX_TOOL_ROUNDS = int(os.getenv("ARGUS_AGENT_MAX_TOOL_ROUNDS", "12"))

# Free tier is rate limited per minute, and a tool loop issues several
# requests back to back — so retry 429s rather than failing the run.
RETRY_ATTEMPTS = int(os.getenv("ARGUS_AGENT_RETRIES", "5"))
RETRY_BASE_DELAY_S = float(os.getenv("ARGUS_AGENT_RETRY_DELAY", "2.0"))

# ── Memory ────────────────────────────────────────────────────────────
MEMORY_DIR = os.getenv("ARGUS_MEMORY_DIR", "/var/lib/argus/memory")
MEMORY_RECALL_LIMIT = int(os.getenv("ARGUS_MEMORY_RECALL_LIMIT", "3"))

# ── Anomaly detector ──────────────────────────────────────────────────
# Rolling z-score: compare the latest value against the mean/stddev of the
# preceding window. Deliberately simple and explainable — see detector.py.
DETECT_WINDOW = os.getenv("ARGUS_DETECT_WINDOW", "30m")
DETECT_STEP = os.getenv("ARGUS_DETECT_STEP", "30s")
DETECT_Z_THRESHOLD = float(os.getenv("ARGUS_DETECT_Z", "3.0"))
DETECT_MIN_SAMPLES = int(os.getenv("ARGUS_DETECT_MIN_SAMPLES", "20"))

# How many trailing points count as "recent" and get scored against the
# baseline behind them. Scoring only the newest point misses any spike that
# already recovered — a 40s chaos run checked a minute later looks perfectly
# healthy. At the default 30s step this is a 3-minute detection window.
DETECT_RECENT_POINTS = int(os.getenv("ARGUS_DETECT_RECENT_POINTS", "6"))
DETECT_INTERVAL_S = int(os.getenv("ARGUS_DETECT_INTERVAL_S", "60"))

# Don't re-investigate the same instance within this window.
DETECT_COOLDOWN_S = int(os.getenv("ARGUS_DETECT_COOLDOWN_S", "900"))

# SLIs worth alerting on. ops_sec is deliberately excluded: throughput
# swings with load by design, so a z-score on it fires on every chaos run
# and every quiet period without indicating anything is wrong.
WATCHED_SLIS = ("argus_latency_ms", "argus_err_rate", "argus_conn_pct")
