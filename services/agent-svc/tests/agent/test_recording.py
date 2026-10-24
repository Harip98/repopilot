"""What the graph writes through its recorder: one tool_calls row per requested call, live
usage counters, and reviewer decisions."""

import pytest

from repopilot.agent.control import CONTROL_TOOLS
from repopilot.agent.state import Budget, RunState
from repopilot.llm import AssistantMessage, ChatResult, ToolCall, Usage
from tests.agent.test_graph import PLAN, Harness, explore_and_edit
from tests.fakes import tool_reply

pytestmark = pytest.mark.anyio


def console_chips(h: Harness) -> list[tuple[list[str], list[str]]]:
    """web/components/Track.tsx's buildStops: each llm.reply takes the next N tool_calls rows,
    N being the number of non-control tool names in the reply."""
    pairs, cursor = [], 0
    for node, kind, payload in h.recorder.events:
        if (node, kind) != ("llm", "reply"):
            continue
        asked = [n for n in payload["tool_calls"] if n not in CONTROL_TOOLS]
        shown = [c.tool for c in h.recorder.tool_calls[cursor : cursor + len(asked)]]
        cursor += len(asked)
        pairs.append((asked, shown))
    return pairs


def _burst(*names: str) -> ChatResult:
    calls = [
        ToolCall(id=f"b{i}", name=n, arguments='{"path": "app/store.py"}')
        for i, n in enumerate(names)
    ]
    return ChatResult(message=AssistantMessage(tool_calls=calls), usage=Usage())


async def test_refused_calls_get_rows_but_are_not_counted():
    h = Harness()
    script = [
        tool_reply("replace_text", path="app/store.py", old="status", new="x"),  # still exploring
        tool_reply("bash", command="ls"),  # no such tool
        tool_reply("finish", summary="too early"),  # a control tool: an event, not a row
        tool_reply("read_file", path="app/store.py"),
        *explore_and_edit()[1:],
    ]
    final = RunState.model_validate(await h.start(script, auto_approve=True))
    rows = [(c.step, c.tool, c.ok, c.refused) for c in h.recorder.tool_calls]
    assert rows == [
        (0, "replace_text", False, True),
        (0, "bash", False, True),
        (1, "read_file", True, False),
        (2, "replace_text", True, False),
    ]
    assert "not available in the explore phase" in h.recorder.tool_calls[0].error
    u = final.usage
    assert (u.tool_calls, u.tool_failures, u.unauthorized_attempts) == (2, 0, 0)
    assert final.status == "completed"


async def test_console_chips_line_up_with_replies():
    h = Harness()
    script = [
        tool_reply("replace_text", path="app/store.py", old="status", new="x"),
        _burst("read_file", "bash", "finish", "list_dir"),
        tool_reply("submit_plan", **PLAN),
        _burst("read_file", "read_file", "read_file"),  # the last two exceed max_tool_calls
    ]
    await h.start(script, budget=Budget(max_tool_calls=3))
    pairs = console_chips(h)
    assert len(pairs) == 4
    for asked, shown in pairs:
        assert shown == asked
    assert [c.refused for c in h.recorder.tool_calls] == [
        True,
        False,
        True,
        False,
        False,
        True,
        True,
    ]


async def test_budget_refusals_recorded_as_refused():
    h = Harness()
    final = RunState.model_validate(
        await h.start([_burst(*["read_file"] * 8)], budget=Budget(max_tool_calls=5))
    )
    assert [c.refused for c in h.recorder.tool_calls] == [False] * 5 + [True] * 3
    assert (
        h.recorder.tool_calls[-1].error == "budget exhausted (tool_calls); this call was not run."
    )
    assert final.usage.tool_calls == 5


# --- live counters --------------------------------------------------------------------------


async def test_counters_written_while_the_run_is_live():
    h = Harness()
    await h.start(explore_and_edit())
    seen = [(u["llm_calls"], u["tool_calls"]) for u in h.recorder.usages]
    # llm, tools, llm, llm, tools, llm: submit_plan and finish change no counter
    assert seen == [(1, 0), (1, 1), (2, 1), (3, 1), (3, 2), (4, 2)]
    assert h.recorder.usages[-1]["prompt_tokens"] == 400
    assert h.recorder.statuses[-1][0] == "awaiting_approval"


async def test_counter_update_precedes_the_reply_event():
    """The console refreshes the run row when an event arrives: the row must already count it."""
    log: list[str] = []
    h = Harness()
    rec = h.recorder
    usage, event = rec.usage, rec.event

    async def logged_usage(run_id, u):
        log.append(f"usage:{u['llm_calls']}")
        await usage(run_id, u)

    async def logged_event(run_id, node, kind, payload):
        log.append(f"{node}.{kind}")
        await event(run_id, node, kind, payload)

    rec.usage, rec.event = logged_usage, logged_event
    await h.start([tool_reply("read_file", path="app/store.py")], budget=Budget(max_llm_calls=1))
    assert log[:2] == ["usage:1", "llm.reply"]


# --- reviewer decisions ---------------------------------------------------------------------


async def test_human_decisions_recorded_per_review_round():
    h = Harness()
    await h.start(explore_and_edit())
    revise = [
        tool_reply(
            "replace_text",
            path="app/store.py",
            old="status.lower() == wanted.lower()",
            new="status.casefold() == wanted.casefold()",
        ),
        tool_reply("finish", summary="casefold"),
    ]
    await h.resume({"decision": "reject", "comment": "use casefold", "by": "bob"}, revise)
    await h.resume({"decision": "approve", "by": "alice"})
    assert h.recorder.approvals == [
        {
            "run_id": "run-1",
            "revision": 0,
            "decision": "reject",
            "comment": "use casefold",
            "by": "bob",
        },
        {"run_id": "run-1", "revision": 1, "decision": "approve", "comment": None, "by": "alice"},
    ]


async def test_plain_rejection_recorded():
    h = Harness()
    await h.start(explore_and_edit())
    final = RunState.model_validate(await h.resume({"decision": "reject", "by": "carol"}))
    assert final.status == "rejected"
    assert [(a["decision"], a["comment"], a["by"]) for a in h.recorder.approvals] == [
        ("reject", None, "carol")
    ]


async def test_auto_approval_is_not_a_reviewer_decision():
    h = Harness()
    final = RunState.model_validate(await h.start(explore_and_edit(), auto_approve=True))
    assert final.approval.by == "auto" and h.recorder.approvals == []
    assert ("approval", "approve") in [(n, k) for n, k, _ in h.recorder.events]


async def test_decision_survives_a_crash_in_the_approval_node():
    """A worker dying after the write re-runs the node with the same decision: one row."""
    h = Harness()
    await h.start(explore_and_edit())
    rec, writes = h.recorder, []
    approval, event = rec.approval, rec.event

    async def counted_approval(run_id, **kw):
        writes.append(kw)
        await approval(run_id, **kw)

    async def dying_event(run_id, node, kind, payload):
        if node == "approval" and not any(n == "approval" for n, _, _ in rec.events):
            rec.event = event  # only the first attempt dies
            raise RuntimeError("worker died")
        await event(run_id, node, kind, payload)

    rec.approval, rec.event = counted_approval, dying_event
    with pytest.raises(RuntimeError, match="worker died"):
        await h.resume({"decision": "approve", "by": "alice"})
    out = await h.graph.ainvoke(None, {"configurable": {"thread_id": "run-1"}}, context=h.deps([]))
    assert RunState.model_validate(out).status == "completed"
    assert len(writes) == 2 and len(rec.approvals) == 1


async def test_memory_recorder_keeps_the_first_decision_of_a_round():
    from repopilot.agent.recorder import MemoryRecorder

    rec = MemoryRecorder()
    await rec.approval("r", revision=0, decision="approve", comment=None, by="a")
    await rec.approval("r", revision=0, decision="approve", comment=None, by="a")  # node re-run
    assert len(rec.approvals) == 1


# --- statuses -------------------------------------------------------------------------------


def test_terminal_statuses_are_reachable():
    from typing import get_args

    from repopilot import worker
    from repopilot.agent import pg_recorder
    from repopilot.agent.state import RunStatus
    from repopilot.api import repo

    terminal = {"completed", "pr_opened", "rejected", "failed"}
    assert pg_recorder.TERMINAL == repo.TERMINAL == worker.TERMINAL == terminal
    assert terminal <= set(get_args(RunStatus))
