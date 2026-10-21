import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from repopilot import telemetry
from repopilot.agent.runner import start_run
from repopilot.config import Settings
from tests.agent.test_graph import Harness, explore_and_edit, make_state

pytestmark = pytest.mark.anyio


@pytest.fixture
def spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "tracer", lambda: provider.get_tracer("test"))
    monkeypatch.setattr(trace, "get_current_span", trace.get_current_span)
    yield exporter
    provider.shutdown()


async def test_run_produces_node_llm_and_tool_spans(spans):
    h = Harness()
    await start_run(make_state(auto_approve=True), h.deps(explore_and_edit()), h.saver)
    names = [s.name for s in spans.get_finished_spans()]
    assert "repopilot.run" in names and "repopilot.node.validate" in names
    assert names.count("gen_ai.chat") == 4
    assert {"repopilot.tool.read_file", "repopilot.tool.replace_text"} <= set(names)
    llm = next(s for s in spans.get_finished_spans() if s.name == "gen_ai.chat")
    assert llm.attributes["gen_ai.usage.input_tokens"] == 100
    assert llm.attributes["gen_ai.request.model"] == "fake"
    run = next(s for s in spans.get_finished_spans() if s.name == "repopilot.run")
    child_traces = {s.context.trace_id for s in spans.get_finished_spans()}
    assert child_traces == {run.context.trace_id}  # one trace per run invocation


async def test_content_not_captured_by_default(spans):
    h = Harness()
    await start_run(make_state(auto_approve=True), h.deps(explore_and_edit()), h.saver)
    for span in spans.get_finished_spans():
        assert "gen_ai.completion" not in span.attributes
        assert "repopilot.tool.output" not in span.attributes


async def test_trace_id_recorded_on_run(spans):
    h = Harness()
    await start_run(make_state(auto_approve=True), h.deps(explore_and_edit()), h.saver)
    running = [fields for status, fields in h.recorder.statuses if status == "running"]
    assert running and len(running[0]["trace_id"]) == 32


def test_setup_disabled_without_endpoint():
    assert telemetry.setup_tracing(Settings(_env_file=None, otel_endpoint=""), "x") is None


def test_langfuse_basic_auth_header():
    s = Settings(_env_file=None, langfuse_public_key="pk-lf-1", langfuse_secret_key="sk-lf-2")
    assert telemetry._otlp_headers(s) == {"Authorization": "Basic cGstbGYtMTpzay1sZi0y"}


async def test_pausing_for_approval_is_not_an_error_span(spans):
    from opentelemetry.trace import StatusCode

    h = Harness()
    await start_run(make_state(), h.deps(explore_and_edit()), h.saver)
    approval = next(s for s in spans.get_finished_spans() if s.name == "repopilot.node.approval")
    assert approval.status.status_code != StatusCode.ERROR
    assert approval.attributes["repopilot.interrupted"] is True and not approval.events
