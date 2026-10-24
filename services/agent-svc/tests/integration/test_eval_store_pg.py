import pytest

from repopilot.bench.tasks import load_tasks
from repopilot.eval.runner import EvalConfigMismatch, PgResultStore, eval_meta, run_eval
from tests.eval.test_runner import FakeRunTask, outcome

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def test_error_rows_are_retried_and_replaced(settings, conn):
    store = PgResultStore(settings.dsn())
    [task] = [t for t in load_tasks() if t.id == "orders-api-001"]
    down = FakeRunTask(
        {task.id: outcome(task.id, status="failed", failure_reason="llm_error: APIConnectionError")}
    )
    [r] = await run_eval("pg-e", [task], down, store, model="m")
    assert r.status == "error"
    up = FakeRunTask({task.id: outcome(task.id)})
    [r] = await run_eval("pg-e", [task], up, store, model="m")
    assert up.calls == [task.id] and r.resolved
    assert (
        conn.execute("SELECT count(*) FROM eval_results WHERE eval_run = 'pg-e'").fetchone()[0] == 1
    )


async def test_shipped_reports_import_into_what_the_console_reads(settings, conn):
    from repopilot.api.repo import RunsRepo
    from repopilot.eval.runner import REPORTS_DIR, import_reports
    from repopilot.eval.score import summarize

    store = PgResultStore(settings.dsn())
    outcomes = await import_reports(store, REPORTS_DIR)
    assert [(name, status) for name, status, _ in outcomes] == [
        ("qwen3b-test", "imported"),
        ("qwen7b-dev", "imported"),
        ("qwen7b-dev2", "imported"),
        ("qwen7b-test", "imported"),
    ]
    evals = {e["name"]: e for e in await RunsRepo(settings.dsn()).eval_names()}  # GET /v1/evals
    assert {n: (e["tasks"], e["resolved"]) for n, e in evals.items()} == {
        "qwen3b-test": (10, 0),
        "qwen7b-dev": (15, 4),
        "qwen7b-dev2": (15, 5),
        "qwen7b-test": (10, 1),
    }
    assert f"{evals['qwen7b-dev']['updated_at']:%Y-%m-%d}" == "2026-10-20"  # the day it ran
    assert summarize(await store.load("qwen7b-test")).resolved == 1  # GET /v1/evals/{name}
    assert (await store.get_meta("qwen7b-dev"))["git_sha"] == "5882df8"

    again = await import_reports(store, REPORTS_DIR)
    assert {status for _, status, _ in again} == {"skipped"}
    assert conn.execute("SELECT count(*) FROM eval_results").fetchone()[0] == 50
    await import_reports(store, REPORTS_DIR, replace=True)
    assert conn.execute("SELECT count(*) FROM eval_results").fetchone()[0] == 50
    assert conn.execute("SELECT count(*) FROM eval_runs").fetchone()[0] == 4


async def test_resume_with_other_model_is_refused(settings, conn):
    store = PgResultStore(settings.dsn())
    tasks = [t for t in load_tasks() if t.id == "orders-api-001"]
    meta = eval_meta("pg-m", "m", "u", tasks, {}, split="dev")
    run_task = FakeRunTask({t.id: outcome(t.id) for t in tasks})
    await run_eval("pg-m", tasks, run_task, store, model="m", meta=meta)
    assert (await store.get_meta("pg-m"))["task_hash"] == meta["task_hash"]
    with pytest.raises(EvalConfigMismatch, match="model"):
        await run_eval("pg-m", tasks, run_task, store, model="x", meta=meta | {"model": "x"})
