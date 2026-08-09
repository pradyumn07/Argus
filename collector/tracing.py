"""OpenTelemetry tracing for the collector.

One span per poll cycle, carrying the instance it targeted and the SLO
verdict it produced. Exported OTLP/HTTP to Tempo.

Why straight to Tempo instead of through an OpenTelemetry Collector: a
Collector earns its keep when you need fan-out to several backends, tail
sampling, or shared processing across many emitting services. With exactly
one producer and one backend it's a hop that can fail without buying
anything, and Tempo speaks OTLP natively. The moment a second service
starts emitting spans (the Phase 4 agent), that calculus flips and the
Collector goes in front.

Tracing is OFF unless OTEL_EXPORTER_OTLP_ENDPOINT is set, so
`cd collector && python main.py` against a bare fleet still works with no
observability stack running at all — same partial-fleet philosophy as
fleet.py skipping unresolvable targets.
"""

import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

SERVICE_NAME = "argus-collector"


def start() -> str | None:
    """Wire up the tracer provider. Returns the endpoint, or None if disabled."""
    base = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not base:
        return None

    endpoint = f"{base.rstrip('/')}/v1/traces"
    provider = TracerProvider(
        resource=Resource.create({"service.name": SERVICE_NAME})
    )
    # Batched, not simple: a span export must never sit in the hot path of
    # a poll loop whose whole job is measuring latency accurately.
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
    )
    trace.set_tracer_provider(provider)
    return endpoint


def tracer() -> trace.Tracer:
    """Always safe to call — returns a no-op tracer when start() was skipped."""
    return trace.get_tracer(SERVICE_NAME)
