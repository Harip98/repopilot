"""A two-task eval against a real sandbox and Postgres, with a scripted model per task."""

import json

import psycopg
import pytest

from repopilot.agent.deps import RunDeps
from repopilot.agent.pg_recorder import PgRecorder
from repopilot.agent.pr import NoPR
from repopilot.agent.runner import postgres_checkpointer, run_bench_task
from repopilot.bench.tasks import load_tasks
from repopilot.eval.runner import PgResultStore, run_eval, write_report
from repopilot.llm import FakeChatModel
from repopilot.sandbox_client import get_sandbox_client
from repopilot.tools.builtin import build_registry
from tests.fakes import tool_reply

pytestmark = [pytest.mark.sandbox, pytest.mark.anyio]

PLAN = {"root_cause": "r", "files": ["app/store.py"], "acceptance_criteria": ["a"]}


def script_for(task) -> list:
    if task.id == "orders-api-005":  # apply the reference fix
        return [tool_reply("submit_plan", **PLAN),
                tool_reply("apply_patch", patch=task.fix_patch.read_text()),
                tool_reply("finish", summary="fixed")]
    return [tool_reply("submit_plan", **PLAN),  # change nothing that matters
            tool_reply("create_file", path="NOTES.md", content="n\n"),
            tool_reply("finish", summary="not fixed")]


async def test_eval_two_tasks_scripted(settings, conn: psycopg.Connection, tmp_path):
    tasks = [t for t in load_tasks() if t.id in ("orders-api-003", "orders-api-005")]
    dsn = settings.dsn()
    recorder = PgRecorder(dsn)
    store = PgResultStore(dsn)
    async with get_sandbox_client() as sandbox, postgres_checkpointer(dsn) as saver:

        async def run_task(task, run_id):
            deps = RunDeps(llm=FakeChatModel(script_for(task)), sandbox=sandbox,
                           registry=build_registry(), recorder=recorder, pr=NoPR())
            return await run_bench_task(task, deps, saver, run_id=run_id, auto_approve=True)

        results = await run_eval("live-e1", tasks, run_task, store, model="fake")
    await recorder.close()

    by_id = {r.task_id: r for r in results}
    assert by_id["orders-api-005"].resolved and not by_id["orders-api-003"].resolved
    assert by_id["orders-api-003"].status == "completed" and by_id["orders-api-003"].heldout_exit_code != 0
    rows = conn.execute("SELECT task_id, resolved, run_id IS NOT NULL FROM eval_results"
                        " WHERE eval_run = 'live-e1' ORDER BY task_id").fetchall()
    assert rows == [("orders-api-003", False, True), ("orders-api-005", True, True)]
    assert await store.load("live-e1") == sorted(results, key=lambda r: r.task_id)

    md = write_report("live-e1", results, {"model": "fake"}, out_dir=tmp_path)
    assert "| Resolution rate | 50.0% (1/2) |" in md.read_text(encoding="utf-8")
    lines = (tmp_path / "live-e1.jsonl").read_text(encoding="utf-8").splitlines()
    assert {json.loads(line)["task_id"] for line in lines} == {"orders-api-003", "orders-api-005"}
