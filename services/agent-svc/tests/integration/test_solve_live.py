"""End to end on a real task: real sandbox, Postgres checkpoints and recorder, scripted model."""

import psycopg
import pytest

from repopilot.agent.deps import RunDeps
from repopilot.agent.pg_recorder import PgRecorder
from repopilot.agent.pr import NoPR
from repopilot.agent.runner import (
    grade,
    new_run_id,
    postgres_checkpointer,
    prepare_bench_run,
    start_run,
)
from repopilot.bench.tasks import load_tasks
from repopilot.llm import FakeChatModel
from repopilot.sandbox_client import get_sandbox_client
from repopilot.tools.builtin import build_registry
from tests.fakes import tool_reply

pytestmark = [pytest.mark.sandbox, pytest.mark.anyio]

PLAN = {
    "root_cause": "status filter compares case-sensitively",
    "files": ["app/store.py"],
    "acceptance_criteria": ["status=PAID matches paid orders"],
}


async def _solve(settings, script: list):
    task = next(t for t in load_tasks() if t.id == "orders-api-005")
    dsn = settings.dsn()
    recorder = PgRecorder(dsn)
    async with get_sandbox_client() as sandbox, postgres_checkpointer(dsn) as saver:
        run_id = new_run_id()
        state = await prepare_bench_run(task, sandbox, run_id=run_id, auto_approve=True)
        await recorder.create_run(
            run_id,
            repo=task.repo,
            issue_text=task.issue,
            task_id=task.id,
            issue_ref=None,
            model="fake",
            task_mode="bench",
            sandbox_id=state.sandbox_id,
        )
        deps = RunDeps(
            llm=FakeChatModel(script),
            sandbox=sandbox,
            registry=build_registry(),
            recorder=recorder,
            pr=NoPR(),
        )
        try:
            result = await start_run(state, deps, saver)
            graded = await grade(task, sandbox, state.sandbox_id)
        finally:
            await sandbox.destroy(state.sandbox_id)
    await recorder.close()
    return run_id, result, graded


async def test_solve_scripted_orders_api_005(settings, conn: psycopg.Connection):
    task = next(t for t in load_tasks() if t.id == "orders-api-005")
    script = [
        tool_reply("search_code", query="status"),
        tool_reply("submit_plan", **PLAN),
        tool_reply("apply_patch", patch=task.fix_patch.read_text()),
        tool_reply("run_tests"),
        tool_reply("finish", summary="lower-case the status filter again"),
    ]
    run_id, result, graded = await _solve(settings, script)
    assert result.state.status == "completed", result.state.failure_reason
    assert result.state.last_tests.passed and "app/store.py" in result.state.diff
    assert graded.resolved, graded.output_tail
    row = conn.execute(
        "SELECT status, llm_calls, tool_calls FROM runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert row == ("completed", 5, 3)
    tools = [
        r[0]
        for r in conn.execute(
            "SELECT tool FROM tool_calls WHERE run_id = %s ORDER BY step", (run_id,)
        ).fetchall()
    ]
    assert tools == ["search_code", "apply_patch", "run_tests"]
    assert (
        conn.execute("SELECT count(*) FROM run_events WHERE run_id = %s", (run_id,)).fetchone()[0]
        >= 6
    )


async def test_unfixed_run_is_not_resolved(settings, conn: psycopg.Connection):
    script = [
        tool_reply("submit_plan", **PLAN),
        tool_reply("create_file", path="NOTES.md", content="looked at it\n"),
        tool_reply("finish", summary="no code change"),
    ]
    _, result, graded = await _solve(settings, script)
    assert result.state.status == "completed"  # visible tests pass on the buggy base
    assert not graded.resolved
