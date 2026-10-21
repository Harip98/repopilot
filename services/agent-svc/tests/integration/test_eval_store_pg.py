import pytest

from repopilot.bench.tasks import load_tasks
from repopilot.eval.runner import EvalConfigMismatch, PgResultStore, eval_meta, run_eval
from tests.eval.test_runner import FakeRunTask, outcome

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def test_error_rows_are_retried_and_replaced(settings, conn):
    store = PgResultStore(settings.dsn())
    [task] = [t for t in load_tasks() if t.id == "orders-api-001"]
    down = FakeRunTask({task.id: outcome(task.id, status="failed",
                                         failure_reason="llm_error: APIConnectionError")})
    [r] = await run_eval("pg-e", [task], down, store, model="m")
    assert r.status == "error"
    up = FakeRunTask({task.id: outcome(task.id)})
    [r] = await run_eval("pg-e", [task], up, store, model="m")
    assert up.calls == [task.id] and r.resolved
    assert conn.execute("SELECT count(*) FROM eval_results WHERE eval_run = 'pg-e'").fetchone()[0] == 1


async def test_resume_with_other_model_is_refused(settings, conn):
    store = PgResultStore(settings.dsn())
    tasks = [t for t in load_tasks() if t.id == "orders-api-001"]
    meta = eval_meta("pg-m", "m", "u", tasks, {}, split="dev")
    run_task = FakeRunTask({t.id: outcome(t.id) for t in tasks})
    await run_eval("pg-m", tasks, run_task, store, model="m", meta=meta)
    assert (await store.get_meta("pg-m"))["task_hash"] == meta["task_hash"]
    with pytest.raises(EvalConfigMismatch, match="model"):
        await run_eval("pg-m", tasks, run_task, store, model="x", meta=meta | {"model": "x"})
