import pytest
from langgraph.checkpoint.memory import InMemorySaver

from repopilot.agent.deps import RunDeps
from repopilot.agent.pr import NoPR
from repopilot.agent.recorder import MemoryRecorder
from repopilot.agent.runner import NotAwaitingApproval, RunNotFound, resume_run, start_run
from repopilot.llm import FakeChatModel
from repopilot.tools.builtin import build_registry
from tests.agent.test_graph import STORE, explore_and_edit, make_state
from tests.fakes import ScriptedSandbox

pytestmark = pytest.mark.anyio


def _deps(sandbox, script) -> RunDeps:
    return RunDeps(llm=FakeChatModel(script), sandbox=sandbox, registry=build_registry(),
                   recorder=MemoryRecorder(), pr=NoPR(), clock=lambda: 1.0)


async def test_start_then_resume_across_graph_instances():
    saver, sandbox = InMemorySaver(), ScriptedSandbox({"app/store.py": STORE})
    started = await start_run(make_state(), _deps(sandbox, explore_and_edit()), saver)
    assert started.interrupt is not None and started.state.status == "running"
    assert "app/store.py" in started.interrupt["diff"]

    resumed = await resume_run("run-1", {"decision": "approve", "by": "alice"},
                               _deps(sandbox, []), saver)
    assert resumed.interrupt is None and resumed.state.status == "completed"


async def test_approve_rejects_wrong_state():
    saver, sandbox = InMemorySaver(), ScriptedSandbox({"app/store.py": STORE})
    with pytest.raises(RunNotFound):
        await resume_run("nope", {"decision": "approve", "by": "a"}, _deps(sandbox, []), saver)
    await start_run(make_state(auto_approve=True), _deps(sandbox, explore_and_edit()), saver)
    with pytest.raises(NotAwaitingApproval):
        await resume_run("run-1", {"decision": "approve", "by": "a"}, _deps(sandbox, []), saver)


async def test_run_bench_task_cleans_up_on_error():
    from repopilot.agent.runner import run_bench_task
    from repopilot.bench.tasks import load_tasks

    class Exploding:
        model = "boom"

        async def chat(self, messages, tools=None, max_tokens=2048):
            raise RuntimeError("unexpected")

    task = next(t for t in load_tasks() if t.id == "orders-api-005")
    sandbox = ScriptedSandbox()
    deps = _deps(sandbox, [])
    deps.llm = Exploding()
    with pytest.raises(RuntimeError):
        await run_bench_task(task, deps, InMemorySaver(), run_id="11111111-1111-1111-1111-111111111111",
                             auto_approve=True)
    assert sandbox.destroyed == ["sbx-1"]
    status, fields = deps.recorder.statuses[-1]
    assert status == "failed" and "unexpected" in fields["error"]


async def test_run_bench_task_grades_and_records_baseline():
    from repopilot.agent.runner import run_bench_task
    from repopilot.bench.tasks import load_tasks

    task = next(t for t in load_tasks() if t.id == "orders-api-005")
    sandbox = ScriptedSandbox({"app/store.py": STORE})
    outcome = await run_bench_task(task, _deps(sandbox, explore_and_edit()), InMemorySaver(),
                                   run_id="22222222-2222-2222-2222-222222222222", auto_approve=True)
    assert outcome.result.state.baseline_tests_passed is True
    assert outcome.graded is not None and sandbox.destroyed == ["sbx-1"]


async def test_run_bench_task_reports_final_diff_when_run_fails_before_validation():
    from repopilot.agent.runner import run_bench_task
    from repopilot.bench.tasks import load_tasks
    from tests.fakes import text_reply

    task = next(t for t in load_tasks() if t.id == "orders-api-005")
    sandbox = ScriptedSandbox({"app/store.py": STORE})
    script = explore_and_edit()[:3] + [text_reply("thinking")] * 5  # edits, then never calls finish
    outcome = await run_bench_task(task, _deps(sandbox, script), InMemorySaver(),
                                   run_id="33333333-3333-3333-3333-333333333333", auto_approve=True)
    assert outcome.result.state.failure_reason == "no_tool_calls"
    assert outcome.result.state.diff == ""  # validation never ran
    assert "app/store.py" in outcome.final_diff and outcome.graded is None


async def test_checkpoints_only_deserialize_registered_state_types(caplog):
    from repopilot.agent.runner import checkpoint_serde
    from tests.agent.test_graph import Harness

    h = Harness(saver=InMemorySaver(serde=checkpoint_serde()))
    await h.start(explore_and_edit())
    with caplog.at_level("WARNING"):
        snap = await h.graph.aget_state({"configurable": {"thread_id": "run-1"}})
    assert snap.next == ("approval",) and snap.values["usage"].llm_calls == 4
    assert "unregistered type" not in caplog.text
