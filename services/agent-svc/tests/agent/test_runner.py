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
