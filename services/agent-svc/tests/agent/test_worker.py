from dataclasses import dataclass, field

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from repopilot.agent.deps import RunDeps
from repopilot.agent.pr import NoPR
from repopilot.agent.recorder import MemoryRecorder
from repopilot.agent.runner import start_run
from repopilot.llm import FakeChatModel
from repopilot.tools.builtin import build_registry
from repopilot.worker import WorkerServices, handle_job
from tests.agent.test_graph import STORE, explore_and_edit, make_state
from tests.fakes import ScriptedSandbox, tool_reply

pytestmark = pytest.mark.anyio


@dataclass
class RunRows(MemoryRecorder):
    """MemoryRecorder plus the run rows the worker reads."""

    rows: dict[str, dict] = field(default_factory=dict)

    async def get_run(self, run_id: str) -> dict | None:
        return self.rows.get(run_id)


def services(sandbox, recorder, saver, script) -> WorkerServices:
    def deps_for(model):
        return RunDeps(llm=FakeChatModel(script), sandbox=sandbox, registry=build_registry(),
                       recorder=recorder, pr=NoPR(), clock=lambda: 1.0)

    return WorkerServices(deps_for=deps_for, checkpointer=saver, recorder=recorder,
                          load_task=lambda task_id: None)


async def test_start_job_idempotent():
    recorder = RunRows(rows={"run-1": {"status": "completed", "model": "m", "task_id": None,
                                       "task_mode": "bench", "sandbox_id": "s"}})
    svc = services(ScriptedSandbox(), recorder, InMemorySaver(), script=[])  # any LLM call fails
    await handle_job({"type": "start", "run_id": "run-1"}, svc)
    assert recorder.statuses == []


async def test_reclaimed_start_continues_from_checkpoint():
    saver, sandbox = InMemorySaver(), ScriptedSandbox({"app/store.py": STORE})
    recorder = RunRows(rows={"run-1": {"status": "running", "model": "m", "task_id": None,
                                       "task_mode": "bench", "sandbox_id": "0123456789abcdef"}})
    crashed = services(sandbox, recorder, saver, explore_and_edit()[:3])
    with pytest.raises(AssertionError):  # the first worker dies mid-run
        await start_run(make_state(auto_approve=True), crashed.deps_for("m"), saver)
    calls_before = len(recorder.tool_calls)

    svc = services(sandbox, recorder, saver, [tool_reply("finish", summary="done")])
    await handle_job({"type": "start", "run_id": "run-1"}, svc)
    assert recorder.statuses[-1][0] == "completed"
    assert len(recorder.tool_calls) == calls_before  # nothing re-run
    assert sandbox.destroyed == ["0123456789abcdef"]


async def test_resume_job_runs_resume():
    saver, sandbox = InMemorySaver(), ScriptedSandbox({"app/store.py": STORE})
    recorder = RunRows(rows={"run-1": {"status": "running", "model": "m", "task_id": None,
                                       "task_mode": "bench", "sandbox_id": "0123456789abcdef"}})
    paused = await start_run(make_state(), services(sandbox, recorder, saver,
                                                    explore_and_edit()).deps_for("m"), saver)
    assert paused.interrupt is not None
    await handle_job({"type": "resume", "run_id": "run-1",
                      "decision": {"decision": "approve", "by": "web"}},
                     services(sandbox, recorder, saver, []))
    assert recorder.statuses[-1][0] == "completed"
    assert sandbox.destroyed == ["0123456789abcdef"]
