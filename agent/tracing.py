"""OpenTelemetry tracing for the agent.

The point of tracing the agent, beyond debugging: an investigation becomes a
trace in Tempo sitting next to the collector polls it reasoned about — the
`agent.investigation` root span with one `agent.tool.*` child per query. You
can open Grafana and watch the agent's reasoning as a waterfall.

Same contract as collector/tracing.py: off unless OTEL_EXPORTER_OTLP_ENDPOINT
is set, so the CLI works with no observability stack running.
"""

import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

SERVICE_NAME = "argus-agent"


def start() -> str | None:
    base = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not base:
        return None
    endpoint = f"{base.rstrip('/')}/v1/traces"
    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    return endpoint
