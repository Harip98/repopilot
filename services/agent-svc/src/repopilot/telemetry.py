"""OpenTelemetry tracing for runs.

Spans: one per run invocation (`repopilot.run`), one per graph node (`repopilot.node.<name>`),
one per model call (`gen_ai.chat`, with GenAI semantic-convention attributes so Langfuse and
other LLM backends show it as a generation) and one per tool call (`repopilot.tool.<name>`).

Export is off unless REPOPILOT_OTEL_ENDPOINT is set: any OTLP/HTTP receiver works, e.g. the
compose `otel` profile's Jaeger at http://127.0.0.1:4318, or Langfuse at
https://cloud.langfuse.com/api/public/otel with REPOPILOT_LANGFUSE_PUBLIC_KEY/SECRET_KEY.
Prompt and tool-output text is not put on spans unless REPOPILOT_OTEL_CAPTURE_CONTENT is true.
"""

import base64
import functools
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import Span, Status, StatusCode

from repopilot.config import Settings

TRACER_NAME = "repopilot"
_capture_content = False


def tracer() -> trace.Tracer:
    return trace.get_tracer(TRACER_NAME)


def capture_content() -> bool:
    return _capture_content


def _otlp_headers(settings: Settings) -> dict[str, str]:
    public, secret = settings.langfuse_public_key, settings.langfuse_secret_key.get_secret_value()
    if public and secret:
        token = base64.b64encode(f"{public}:{secret}".encode()).decode()
        return {"Authorization": f"Basic {token}"}
    return {}


def setup_tracing(settings: Settings, service_name: str,
                  exporter: SpanExporter | None = None) -> TracerProvider | None:
    """Install a tracer provider; returns None (tracing stays a no-op) when nothing is configured."""
    global _capture_content
    _capture_content = settings.otel_capture_content
    if exporter is None:
        if not settings.otel_endpoint:
            return None
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        endpoint = settings.otel_endpoint.rstrip("/")
        if not endpoint.endswith("/v1/traces"):
            endpoint += "/v1/traces"
        exporter = OTLPSpanExporter(endpoint=endpoint, headers=_otlp_headers(settings))
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return provider


def trace_id_hex(span: Span) -> str | None:
    ctx = span.get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


@contextmanager
def run_span(name: str, run_id: str, **attrs: Any) -> Iterator[Span]:
    with tracer().start_as_current_span(name, attributes={"repopilot.run_id": run_id,
                                                          **_clean(attrs)}) as span:
        yield span


def _clean(attrs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in attrs.items() if v is not None}


def traced_node(name: str, fn: Callable[..., Awaitable[dict]]) -> Callable[..., Awaitable[dict]]:
    """Wrap a graph node in a span. functools.wraps keeps the signature LangGraph inspects to
    inject the runtime context."""

    @functools.wraps(fn)
    async def wrapper(state, runtime):
        with tracer().start_as_current_span(f"repopilot.node.{name}", attributes={
            "repopilot.run_id": state.run_id, "repopilot.phase": state.phase,
        }) as span:
            update = await fn(state, runtime)
            if route := update.get("route"):
                span.set_attribute("repopilot.route", route)
            if reason := update.get("failure_reason"):
                span.set_attribute("repopilot.failure_reason", reason)
            return update

    return wrapper


@contextmanager
def llm_span(model: str, run_id: str, messages: int) -> Iterator[Span]:
    with tracer().start_as_current_span("gen_ai.chat", attributes={
        "gen_ai.operation.name": "chat", "gen_ai.system": "openai",
        "gen_ai.request.model": model, "gen_ai.request.temperature": 0.0,
        "repopilot.run_id": run_id, "repopilot.messages": messages,
    }) as span:
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise


def record_llm_result(span: Span, prompt_tokens: int, completion_tokens: int,
                      finish_reason: str | None, tool_calls: list[str], content: str | None) -> None:
    span.set_attributes(_clean({
        "gen_ai.usage.input_tokens": prompt_tokens, "gen_ai.usage.output_tokens": completion_tokens,
        "gen_ai.response.finish_reasons": [finish_reason] if finish_reason else None,
        "repopilot.tool_calls": tool_calls,
        "gen_ai.completion": (content or "")[:4000] if capture_content() else None,
    }))


@contextmanager
def tool_span(name: str, run_id: str) -> Iterator[Span]:
    with tracer().start_as_current_span(f"repopilot.tool.{name}", attributes={
        "repopilot.run_id": run_id, "repopilot.tool": name}) as span:
        yield span


def record_tool_result(span: Span, ok: bool, denied: bool, error: str | None, latency_ms: int,
                       content: str) -> None:
    span.set_attributes(_clean({
        "repopilot.tool.ok": ok, "repopilot.tool.denied": denied, "repopilot.tool.latency_ms": latency_ms,
        "repopilot.tool.error": error, "repopilot.tool.output": content[:2000] if capture_content() else None,
    }))
    if not ok:
        span.set_status(Status(StatusCode.ERROR, "denied" if denied else "failed"))
