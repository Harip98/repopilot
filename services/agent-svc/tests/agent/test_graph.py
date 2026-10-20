import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from repopilot.agent.deps import RunDeps
from repopilot.agent.graph import build_graph
from repopilot.agent.pr import NoPR
from repopilot.agent.recorder import MemoryRecorder
from repopilot.agent.state import Budget, RunState
from repopilot.llm import FakeChatModel
from repopilot.tools.builtin import build_registry
from tests.fakes import ScriptedSandbox, text_reply, tool_reply

pytestmark = pytest.mark.anyio

STORE = b"def matches(status, wanted):\n    return status == wanted\n"
PLAN = {"root_cause": "case-sensitive compare", "files": ["app/store.py"],
        "acceptance_criteria": ["PAID matches paid"], "test_targets": ["tests"]}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def explore_and_edit() -> list:
    return [
        tool_reply("read_file", path="app/store.py"),
        tool_reply("submit_plan", **PLAN),
        tool_reply("replace_text", path="app/store.py", old="status == wanted",
                   new="status.lower() == wanted.lower()"),
        tool_reply("finish", summary="compare case-insensitively"),
    ]


def make_state(**kw) -> RunState:
    base = {"run_id": "run-1", "task_id": "orders-api-005", "repo": "orders-api",
            "issue_title": "Status filter is case-sensitive", "issue_text": "PAID finds nothing",
            "sandbox_id": "0123456789abcdef", "test_command": ["pytest", "-q"],
            "protected_paths": ["tests/test_heldout_*"], "started_at": 0.0}
    return RunState(**(base | kw))


class Harness:
    def __init__(self, sandbox: ScriptedSandbox | None = None, saver=None):
        self.sandbox = sandbox or ScriptedSandbox({"app/store.py": STORE})
        self.recorder = MemoryRecorder()
        self.saver = saver or InMemorySaver()
        self.graph = build_graph().compile(checkpointer=self.saver)

    def deps(self, script: list) -> RunDeps:
        self.llm = FakeChatModel(script)
        return RunDeps(llm=self.llm, sandbox=self.sandbox, registry=build_registry(),
                       recorder=self.recorder, pr=NoPR(), clock=lambda: 1.0)

    async def start(self, script: list, **state_kw) -> dict:
        state = make_state(**state_kw)
        return await self.graph.ainvoke(state, {"configurable": {"thread_id": state.run_id}},
                                        context=self.deps(script))

    async def resume(self, value: dict, script: list | None = None, run_id: str = "run-1") -> dict:
        return await self.graph.ainvoke(Command(resume=value),
                                        {"configurable": {"thread_id": run_id}},
                                        context=self.deps(script or []))


async def test_happy_path():
    h = Harness()
    out = await h.start(explore_and_edit())
    [interrupt] = out["__interrupt__"]
    payload = interrupt.value
    assert payload["run_id"] == "run-1" and "app/store.py" in payload["diff"]
    assert payload["tests"]["exit_code"] == 0 and payload["plan"]["files"] == ["app/store.py"]
    assert payload["summary"] == "compare case-insensitively"
    assert h.recorder.statuses[-1][0] == "awaiting_approval"

    final = RunState.model_validate(await h.resume({"decision": "approve", "by": "alice"}))
    assert final.status == "completed" and final.approval.by == "alice"
    assert b"status.lower()" in h.sandbox.files["app/store.py"]
    assert final.usage.llm_calls == 4 and final.usage.tool_calls == 2
    assert [c.tool for c in h.recorder.tool_calls] == ["read_file", "replace_text"]
    assert h.recorder.statuses[-1][0] == "completed"


async def test_test_failure_diagnose_then_fix():
    h = Harness()
    h.sandbox.test_results = [(1, "FAILED tests/test_x.py::test_case - AssertionError"), (0, "ok")]
    script = explore_and_edit() + [
        tool_reply("run_tests"),
        tool_reply("finish", summary="second try"),
    ]
    out = await h.start(script, auto_approve=True)
    final = RunState.model_validate(out)
    assert final.status == "completed" and final.retries == 1
    diagnose = [m for m in final.messages if m["role"] == "user" and "Validation failed" in m["content"]]
    assert len(diagnose) == 1 and "AssertionError" in diagnose[0]["content"]


async def test_retries_exhausted_fails():
    h = Harness()
    h.sandbox.test_results = [(1, "boom")] * 3
    script = explore_and_edit() + [tool_reply("finish", summary="again")] * 2
    final = RunState.model_validate(
        await h.start(script, auto_approve=True, budget=Budget(max_retries=2)))
    assert final.status == "failed" and final.failure_reason == "tests_failing"
    assert final.retries == 2


async def test_budget_exhausted():
    h = Harness()
    final = RunState.model_validate(
        await h.start(explore_and_edit(), budget=Budget(max_llm_calls=3)))
    assert final.status == "failed" and final.failure_reason == "budget:llm_calls"
    assert h.recorder.statuses[-1][0] == "failed"


async def test_policy_violation_counted():
    h = Harness()
    script = [
        tool_reply("submit_plan", **PLAN),
        tool_reply("create_file", path=".github/workflows/x.yml", content="on: push"),
        tool_reply("replace_text", path="app/store.py", old="status == wanted", new="True"),
        tool_reply("finish", summary="done"),
    ]
    final = RunState.model_validate(await h.start(script, auto_approve=True))
    assert final.usage.unauthorized_attempts == 1 and final.status == "completed"
    denied = [c for c in h.recorder.tool_calls if c.denied]
    assert [c.tool for c in denied] == ["create_file"]


async def test_tool_outside_phase_is_refused():
    h = Harness()
    script = [
        tool_reply("replace_text", path="app/store.py", old="status", new="x"),  # still exploring
        tool_reply("submit_plan", **PLAN),
        tool_reply("finish", summary="nothing"),
    ]
    out = await h.start(script, budget=Budget(max_retries=0))
    final = RunState.model_validate(out)
    assert h.sandbox.files["app/store.py"] == STORE
    refused = final.messages[3]
    assert refused["role"] == "tool" and "not available" in refused["content"]


async def test_text_only_replies_fail_after_nudges():
    h = Harness()
    final = RunState.model_validate(await h.start([text_reply("I think it is fixed.")] * 3))
    assert final.status == "failed" and final.failure_reason == "no_tool_calls"
    assert final.nudges == 2 and final.usage.llm_calls == 3


async def test_finish_without_changes():
    h = Harness()
    script = [
        tool_reply("submit_plan", **PLAN),
        tool_reply("finish", summary="nothing to do"),
        tool_reply("replace_text", path="app/store.py", old="status == wanted", new="True"),
        tool_reply("finish", summary="now changed"),
    ]
    final = RunState.model_validate(await h.start(script, auto_approve=True))
    assert final.status == "completed" and final.retries == 1
    assert any("not changed any files" in m.get("content") or "" for m in final.messages
               if m["role"] == "user")


async def test_validate_rejects_protected_changes():
    h = Harness()
    h.sandbox.changed.append("tests/test_heldout_status.py")  # e.g. written by test code
    final = RunState.model_validate(await h.start(explore_and_edit(), auto_approve=True))
    assert final.status == "failed" and final.failure_reason == "protected_paths_changed"
    assert final.last_tests.violations == ["tests/test_heldout_status.py"]
    assert final.usage.unauthorized_attempts == 1


async def test_reject_with_comment_revises_once():
    h = Harness()
    await h.start(explore_and_edit())
    revise = [tool_reply("replace_text", path="app/store.py", old="status.lower() == wanted.lower()",
                         new="status.casefold() == wanted.casefold()"),
              tool_reply("finish", summary="casefold")]
    out = await h.resume({"decision": "reject", "comment": "use casefold", "by": "bob"}, revise)
    assert "__interrupt__" in out
    assert any("use casefold" in (m.get("content") or "") for m in out["messages"])
    final = RunState.model_validate(
        await h.resume({"decision": "reject", "comment": "still no", "by": "bob"}))
    assert final.status == "rejected" and final.revisions == 1


async def test_resume_after_crash():
    saver = InMemorySaver()
    first = Harness(saver=saver)
    crashing = explore_and_edit()[:3]  # read_file, submit_plan, replace_text; then the model dies
    with pytest.raises(AssertionError, match="script exhausted"):
        await first.start(crashing, auto_approve=True)
    assert [c.tool for c in first.recorder.tool_calls] == ["read_file", "replace_text"]

    second = Harness(sandbox=first.sandbox, saver=saver)
    out = await second.graph.ainvoke(None, {"configurable": {"thread_id": "run-1"}},
                                     context=second.deps([tool_reply("finish", summary="done")]))
    final = RunState.model_validate(out)
    assert final.status == "completed"
    assert second.recorder.tool_calls == []  # nothing before the crash was re-run
    assert final.usage.llm_calls == 4


async def test_auto_approve_skips_interrupt():
    h = Harness()
    out = await h.start(explore_and_edit(), auto_approve=True)
    assert "__interrupt__" not in out
    final = RunState.model_validate(out)
    assert final.approval.by == "auto" and final.status == "completed"
