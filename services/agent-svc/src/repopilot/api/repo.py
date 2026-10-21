"""Read and write the run tables for the API. One short-lived connection per call keeps the
API stateless; the agent itself writes through PgRecorder."""

from typing import Any

import psycopg
from psycopg.rows import dict_row

TERMINAL = {"completed", "pr_opened", "rejected", "failed", "cancelled"}
_LIST_COLUMNS = (
    "id::text AS id, task_id, repo, issue_ref, status, failure_reason, model, retries, llm_calls,"
    " tool_calls, tool_failures, unauthorized_attempts, prompt_tokens, completion_tokens,"
    " cost_usd::float AS cost_usd, pr_url, created_at, updated_at, finished_at"
)


class RunsRepo:
    def __init__(self, dsn: str):
        self._dsn = dsn

    async def _fetch(self, sql: str, params: tuple = ()) -> list[dict]:
        async with await psycopg.AsyncConnection.connect(self._dsn, row_factory=dict_row) as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchall()

    async def create_queued_run(self, run_id: str, *, repo: str, issue_text: str,
                                task_id: str | None, issue_ref: str | None, task_mode: str,
                                model: str) -> None:
        async with await psycopg.AsyncConnection.connect(self._dsn, autocommit=True) as conn:
            await conn.execute(
                "INSERT INTO runs (id, repo, issue_text, task_id, issue_ref, task_mode, model, status)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, 'queued')",
                (run_id, repo, issue_text, task_id, issue_ref, task_mode, model),
            )

    async def list_runs(self, limit: int) -> list[dict]:
        return await self._fetch(
            f"SELECT {_LIST_COLUMNS} FROM runs ORDER BY created_at DESC LIMIT %s", (limit,))

    async def get_run(self, run_id: str) -> dict | None:
        rows = await self._fetch(
            f"SELECT {_LIST_COLUMNS}, issue_text, task_mode, error, approval_request, result"
            " FROM runs WHERE id::text = %s", (run_id,))
        return rows[0] if rows else None

    async def status(self, run_id: str) -> str | None:
        rows = await self._fetch("SELECT status FROM runs WHERE id::text = %s", (run_id,))
        return rows[0]["status"] if rows else None

    async def events_after(self, run_id: str, after_seq: int, limit: int = 200) -> list[dict]:
        return await self._fetch(
            "SELECT seq, node, kind, payload, created_at FROM run_events"
            " WHERE run_id::text = %s AND seq > %s ORDER BY seq LIMIT %s",
            (run_id, after_seq, limit))

    async def tool_calls(self, run_id: str) -> list[dict]:
        return await self._fetch(
            "SELECT step, tool, args, ok, denied, error, result_excerpt, latency_ms, created_at"
            " FROM tool_calls WHERE run_id::text = %s ORDER BY step, id", (run_id,))

    async def eval_names(self) -> list[dict[str, Any]]:
        return await self._fetch(
            "SELECT eval_run AS name, min(model) AS model, count(*) AS tasks,"
            " sum(resolved::int) AS resolved, max(created_at) AS updated_at"
            " FROM eval_results GROUP BY eval_run ORDER BY max(created_at) DESC")
