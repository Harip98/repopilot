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
        self.now = 1.0

    def deps(self, script: list) -> RunDeps:
        self.llm = FakeChatModel(script)
        return RunDeps(llm=self.llm, sandbox=self.sandbox, registry=build_registry(),
                       recorder=self.recorder, pr=NoPR(), clock=lambda: self.now)

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


# --- validate against real git (review C1/I6) ---------------------------------------------

GIT_FILES = {"app/store.py": STORE, "tests/test_store.py": b"def test_x():\n    pass\n",
             "tests/conftest.py": b"import pytest\n"}
PROTECTED = ["tests/test_heldout_*", "conftest.py", "*/conftest.py"]


def git_harness(tmp_path) -> Harness:
    from tests.fakes import GitDirSandbox

    return Harness(sandbox=GitDirSandbox(tmp_path, GIT_FILES))


async def test_validate_sees_deletions(tmp_path):
    h = git_harness(tmp_path)
    delete = ("diff --git a/tests/test_store.py b/tests/test_store.py\ndeleted file mode 100644\n"
              "--- a/tests/test_store.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-def test_x():\n-    pass\n")
    script = [tool_reply("submit_plan", **PLAN), tool_reply("apply_patch", patch=delete),
              tool_reply("finish", summary="removed a test")]
    out = await h.start(script, protected_paths=PROTECTED)
    payload = out["__interrupt__"][0].value
    assert "deleted file mode" in payload["diff"] and "tests/test_store.py" in payload["diff"]


async def test_validate_rejects_protected_rename_by_test_code(tmp_path):
    h = git_harness(tmp_path)
    h.sandbox.on_test = lambda root: (root / "tests" / "conftest.py").rename(root / "tests" / "moved.py")
    script = explore_and_edit()
    final = RunState.model_validate(await h.start(script, protected_paths=PROTECTED, auto_approve=True))
    assert final.status == "failed" and final.failure_reason == "protected_paths_changed"
    assert "tests/conftest.py" in final.last_tests.violations


async def test_validate_sees_ignored_files(tmp_path):
    def plant(root):
        (root / ".gitignore").write_bytes(b"conftest.py\n")
        (root / "conftest.py").write_bytes(b"# forces passes\n")

    h = git_harness(tmp_path)
    h.sandbox.on_test = plant
    final = RunState.model_validate(
        await h.start(explore_and_edit(), protected_paths=PROTECTED, auto_approve=True))
    assert final.status == "failed" and "conftest.py" in final.last_tests.violations


# --- budgets (review I1/I2) ---------------------------------------------------------------


async def test_one_turn_cannot_exceed_tool_budget():
    from repopilot.llm import AssistantMessage, ChatResult, ToolCall, Usage

    calls = [ToolCall(id=f"c{i}", name="read_file", arguments='{"path": "app/store.py"}')
             for i in range(30)]
    burst = ChatResult(message=AssistantMessage(tool_calls=calls), usage=Usage())
    h = Harness()
    final = RunState.model_validate(await h.start([burst], budget=Budget(max_tool_calls=5)))
    assert len(h.recorder.tool_calls) == 5
    assert final.status == "failed" and final.failure_reason == "budget:tool_calls"
    skipped = [m for m in final.messages if m["role"] == "tool" and "budget exhausted" in m["content"]]
    assert len(skipped) == 25


async def test_review_wait_not_counted_against_time_budget():
    h = Harness()
    await h.start(explore_and_edit(), budget=Budget(max_seconds=100))
    h.now = 5000.0  # the reviewer answered over an hour later
    revise = [tool_reply("replace_text", path="app/store.py", old="status.lower() == wanted.lower()",
                         new="status.casefold() == wanted.casefold()"),
              tool_reply("finish", summary="casefold")]
    out = await h.resume({"decision": "reject", "comment": "use casefold", "by": "bob"}, revise)
    assert "__interrupt__" in out, out.get("failure_reason")
    assert b"casefold" in h.sandbox.files["app/store.py"]


# --- failures become outcomes (review I4) -------------------------------------------------


class DownModel:
    model = "down"

    async def chat(self, messages, tools=None, max_tokens=2048):
        import httpx
        import openai

        raise openai.APIConnectionError(request=httpx.Request("POST", "http://127.0.0.1:1/v1"))


async def test_llm_api_error_fails_run():
    h = Harness()
    deps = h.deps([])
    deps.llm = DownModel()
    state = make_state()
    out = await h.graph.ainvoke(state, {"configurable": {"thread_id": "run-1"}}, context=deps)
    final = RunState.model_validate(out)
    assert final.status == "failed" and final.failure_reason == "llm_error: APIConnectionError"
    assert h.recorder.statuses[-1][0] == "failed"


async def test_sandbox_error_in_validate_fails_run():
    from repopilot.sandbox_client import SandboxError

    h = Harness()

    async def broken_exec(sandbox_id, argv, timeout_s=120, cwd=None):
        raise SandboxError("not_found", "no such sandbox", 404)

    final = None
    original = h.sandbox.exec

    async def exec_until_validate(sandbox_id, argv, timeout_s=120, cwd=None):
        if argv[:3] == ["git", "add", "--intent-to-add"]:
            return await broken_exec(sandbox_id, argv)
        return await original(sandbox_id, argv, timeout_s, cwd)

    h.sandbox.exec = exec_until_validate
    final = RunState.model_validate(await h.start(explore_and_edit(), auto_approve=True))
    assert final.status == "failed" and final.failure_reason == "sandbox_error: not_found"


async def test_intake_includes_listing():
    h = Harness()
    out = await h.start([text_reply("x")] * 3)
    issue = out["messages"][1]["content"]
    assert "Repository files" in issue and "app/store.py" in issue
    assert h.recorder.tool_calls == []  # the listing is context, not an agent tool call
