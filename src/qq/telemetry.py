"""Opt-in OpenTelemetry tracing.

Off unless ``QQ_OTEL=1``. qq makes exactly one HTTP call, so paying the import
cost of the OpenTelemetry SDK on every invocation would be a poor trade; this
module is only imported when the environment variable is set.

Server-side observability is the more useful half and needs no code: point a
diagnostic setting on the Foundry account at a Log Analytics workspace (see
``logAnalyticsWorkspaceId`` in infra/main.bicep) to get per-request latency and
token counts, including which model the router picked.
"""

from __future__ import annotations

import os

#: Required by the GenAI instrumentation before it will emit Responses API spans.
_SEMCONV_OPT_IN = "gen_ai_latest_experimental"


def enabled() -> bool:
    return os.environ.get("QQ_OTEL", "").strip().lower() in ("1", "true", "yes", "on")


def setup() -> bool:
    """Wire up tracing. Returns True if instrumentation was installed.

    Never raises: telemetry must not be able to break the answer.
    """
    if not enabled():
        return False

    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", _SEMCONV_OPT_IN)
    os.environ.setdefault("OTEL_SERVICE_NAME", "qq")

    try:
        from opentelemetry import trace
        from opentelemetry.instrumentation.genai.openai import OpenAIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        return False

    try:
        provider = TracerProvider(resource=Resource.create({"service.name": "qq"}))
        if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        OpenAIInstrumentor().instrument()
    except Exception:
        return False
    return True
