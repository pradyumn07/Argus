"""Structured JSON logging for the collector.

One JSON object per line on stdout. Grafana Alloy tails the container's
stdout and ships it to Loki, where these fields become queryable LogQL
selectors instead of substrings someone has to regex out of a sentence.

The important field is `trace_id`: it's pulled from whatever OTel span is
active when the record is emitted, which is what lets Grafana turn a log
line into a one-click jump to the matching Tempo trace. That only works if
the log call happens *inside* the span — see poll_loop() in main.py.

stdout stays the transport (never a file, never a direct push to Loki):
the collector shouldn't know or care that Loki exists, and both Docker and
Kubernetes already solve "collect a container's stdout" better than an
in-process shipper would.
"""

import datetime
import json
import logging
import sys

from opentelemetry import trace

LOGGER_NAME = "argus.collector"

# Attributes the stdlib puts on every LogRecord. Anything NOT in here that
# a caller passed via extra= is application data worth emitting.
_STDLIB_FIELDS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"asctime", "message", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            # RFC3339/UTC with milliseconds — what Loki and Grafana expect.
            "ts": datetime.datetime.fromtimestamp(
                record.created, datetime.timezone.utc
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
        }

        # Flatten anything passed as logger.info(..., extra={...}).
        for key, value in record.__dict__.items():
            if key not in _STDLIB_FIELDS and not key.startswith("_"):
                payload[key] = value

        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            payload["trace_id"] = format(span_context.trace_id, "032x")
            payload["span_id"] = format(span_context.span_id, "016x")

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def setup(level: int = logging.INFO) -> logging.Logger:
    """Install the JSON formatter on the root logger, return the app logger."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()   # drop any default/basicConfig handler
    root.addHandler(handler)
    root.setLevel(level)

    # These libraries are chatty at INFO and their noise would drown the
    # poll events in Loki. Warnings and errors still come through.
    for noisy in ("pymongo", "motor", "asyncio", "urllib3", "opentelemetry"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger(LOGGER_NAME)
