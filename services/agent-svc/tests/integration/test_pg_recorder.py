import uuid

import anyio
import psycopg
import pytest

from repopilot.agent.pg_recorder import PgRecorder
from repopilot.llm import ToolCall
from repopilot.tools.registry import ToolOutcome

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
async def recorder(settings, conn):  # `conn` truncates the tables after each test
    rec = PgRecorder(settings.dsn())
    yield rec
    await rec.close()


async def _new_run(rec: PgRecorder) -> str:
    run_id = str(uuid.uuid4())
    await rec.create_run(run_id, repo="orders-api", issue_text="PAID finds nothing",
                         task_id="orders-api-005", issue_ref=None, model="fake", task_mode="bench")
    return run_id


async def test_status_lifecycle(recorder, conn: psycopg.Connection):
    run_id = await _new_run(recorder)
    await recorder.status(run_id, "running")
    await recorder.status(run_id, "awaiting_approval")
    usage = {"llm_calls": 4, "tool_calls": 2, "tool_failures": 1, "unauthorized_attempts": 1,
             "prompt_tokens": 900, "completion_tokens": 80, "cost_usd": 0.0012}
    await recorder.status(run_id, "completed", failure_reason=None, pr_url=None, retries=1,
                          usage=usage)
    row = conn.execute(
        "SELECT status, retries, llm_calls, tool_calls, tool_failures, unauthorized_attempts,"
        " prompt_tokens, completion_tokens, cost_usd::float, finished_at IS NOT NULL,"
        " updated_at > created_at FROM runs WHERE id = %s", (run_id,)).fetchone()
    assert row == ("completed", 1, 4, 2, 1, 1, 900, 80, 0.0012, True, True)


async def test_failed_records_reason(recorder, conn: psycopg.Connection):
    run_id = await _new_run(recorder)
    await recorder.status(run_id, "failed", failure_reason="budget:llm_calls", retries=0, usage={})
    assert conn.execute("SELECT status, failure_reason FROM runs WHERE id = %s",
                        (run_id,)).fetchone() == ("failed", "budget:llm_calls")


async def test_events_sequenced(recorder, settings, conn: psycopg.Connection):
    run_id = await _new_run(recorder)
    other = PgRecorder(settings.dsn())
    try:
        async with anyio.create_task_group() as tg:
            for i in range(10):
                rec = recorder if i % 2 else other
                tg.start_soon(rec.event, run_id, "llm", "reply", {"i": i})
    finally:
        await other.close()
    seqs = [r[0] for r in conn.execute(
        "SELECT seq FROM run_events WHERE run_id = %s ORDER BY seq", (run_id,)).fetchall()]
    assert seqs == list(range(1, 11))


async def test_tool_calls_recorded(recorder, conn: psycopg.Connection):
    run_id = await _new_run(recorder)
    ok = ToolOutcome(ok=True, content="x" * 5000, latency_ms=12)
    denied = ToolOutcome(ok=False, denied=True, error="policy: .github/ is protected", latency_ms=1)
    await recorder.tool_call(run_id, 1, ToolCall(id="c1", name="read_file", arguments='{"path": "a.py"}'), ok)
    await recorder.tool_call(run_id, 2, ToolCall(id="c2", name="apply_patch", arguments="not json"), denied)
    rows = conn.execute(
        "SELECT step, tool, args, ok, denied, error, length(result_excerpt), latency_ms"
        " FROM tool_calls WHERE run_id = %s ORDER BY step", (run_id,)).fetchall()
    assert rows[0] == (1, "read_file", {"path": "a.py"}, True, False, None, 2000, 12)
    assert rows[1][:6] == (2, "apply_patch", {"raw": "not json"}, False, True,
                           "policy: .github/ is protected")


def test_completed_status_allowed(conn: psycopg.Connection):
    run_id = conn.execute(
        "INSERT INTO runs (repo, issue_text, status, task_mode) VALUES ('r', 'i', 'completed',"
        " 'bench') RETURNING id").fetchone()[0]
    assert run_id is not None
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO runs (repo, issue_text, task_mode) VALUES ('r', 'i', 'nope')")


async def test_claim_for_approval_is_exclusive(recorder, settings, conn: psycopg.Connection):
    run_id = await _new_run(recorder)
    await recorder.status(run_id, "awaiting_approval")
    other = PgRecorder(settings.dsn())
    try:
        results = []

        async def claim(rec):
            results.append(await rec.claim_for_approval(run_id))

        async with anyio.create_task_group() as tg:
            tg.start_soon(claim, recorder)
            tg.start_soon(claim, other)
    finally:
        await other.close()
    winners = [r for r in results if r is not None]
    assert len(winners) == 1 and winners[0]["task_id"] == "orders-api-005"
    assert conn.execute("SELECT status FROM runs WHERE id = %s", (run_id,)).fetchone()[0] == "running"
    assert await recorder.claim_for_approval(str(uuid.uuid4())) is None
