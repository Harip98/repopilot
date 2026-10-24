"""The tools node enforces exactly what the llm node offered for the reply it executes."""

import pytest

from repopilot.agent.state import Budget, RunState
from tests.agent.test_graph import PLAN, Harness
from tests.fakes import tool_reply

pytestmark = pytest.mark.anyio

READ = {"read_file", "list_dir", "search_code", "git_log", "git_diff", "submit_plan"}


def _offered(h: Harness, reply: int) -> set[str]:
    return {s.name for s in h.llm.calls[reply - 1]["tools"]}


def _tool_results(final: RunState) -> list[str]:
    return [m["content"] for m in final.messages if m["role"] == "tool"]


async def test_fifteenth_explore_reply_runs_its_read_tools():
    h = Harness()
    script = [tool_reply("read_file", path="app/store.py")] * 15 + [
        tool_reply("submit_plan", **PLAN),
        tool_reply(
            "replace_text",
            path="app/store.py",
            old="status == wanted",
            new="status.lower() == wanted.lower()",
        ),
        tool_reply("finish", summary="compare case-insensitively"),
    ]
    final = RunState.model_validate(await h.start(script, auto_approve=True))
    assert _offered(h, 15) == READ and _offered(h, 16) == {"submit_plan"}
    assert not [c for c in _tool_results(final) if c.startswith("ERROR")]
    ran = [c.tool for c in h.recorder.tool_calls if not c.refused]
    assert ran == ["read_file"] * 15 + ["replace_text"]
    assert final.status == "completed" and final.usage.tool_calls == 16


async def test_read_tools_refused_once_exploration_is_spent():
    h = Harness()
    script = [tool_reply("read_file", path="app/store.py")] * 16 + [
        tool_reply("submit_plan", **PLAN),
        tool_reply(
            "replace_text",
            path="app/store.py",
            old="status == wanted",
            new="status.lower() == wanted.lower()",
        ),
        tool_reply("finish", summary="compare case-insensitively"),
    ]
    final = RunState.model_validate(await h.start(script, auto_approve=True))
    results = _tool_results(final)
    assert results[15] == (
        "ERROR: tool 'read_file' is not available in the explore phase; available: submit_plan"
    )
    assert final.usage.tool_calls == 16 and final.status == "completed"


async def test_last_allowed_model_call_runs_its_tools():
    h = Harness()  # default budget: 40 model calls
    script = (
        [tool_reply("read_file", path="app/store.py")] * 15
        + [tool_reply("submit_plan", **PLAN)]
        + [tool_reply("read_file", path="app/store.py")] * 24
    )
    final = RunState.model_validate(await h.start(script))
    assert final.usage.llm_calls == 40 and final.usage.tool_calls == 39
    assert not [c for c in _tool_results(final) if "budget exhausted" in c]
    assert final.status == "failed" and final.failure_reason == "budget:llm_calls"


async def test_small_llm_budget_last_reply_edits():
    h = Harness()
    script = [
        tool_reply("read_file", path="app/store.py"),
        tool_reply("submit_plan", **PLAN),
        tool_reply(
            "replace_text",
            path="app/store.py",
            old="status == wanted",
            new="status.lower() == wanted.lower()",
        ),
    ]
    final = RunState.model_validate(await h.start(script, budget=Budget(max_llm_calls=3)))
    assert [c.tool for c in h.recorder.tool_calls] == ["read_file", "replace_text"]
    assert b"status.lower()" in h.sandbox.files["app/store.py"]
    assert final.failure_reason == "budget:llm_calls"


async def test_submit_plan_unlocks_edit_tools_within_the_same_reply():
    from repopilot.llm import AssistantMessage, ChatResult, ToolCall, Usage

    plan = tool_reply("submit_plan", **PLAN).message.tool_calls[0]
    edit = ToolCall(
        id="e1",
        name="replace_text",
        arguments='{"path": "app/store.py", "old": "status == wanted", '
        '"new": "status.lower() == wanted.lower()"}',
    )
    both = ChatResult(message=AssistantMessage(tool_calls=[plan, edit]), usage=Usage())
    h = Harness()
    final = RunState.model_validate(
        await h.start([both, tool_reply("finish", summary="done")], auto_approve=True)
    )
    assert final.status == "completed" and b"status.lower()" in h.sandbox.files["app/store.py"]
