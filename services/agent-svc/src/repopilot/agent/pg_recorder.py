"""Recorder backed by Postgres: the runs row, its ordered event log, every tool call, and the
reviewers' decisions."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import psycopg
from psycopg.types.json import Jsonb

from repopilot.llm.types import ToolCall
from repopilot.tools.registry import ToolOutcome

TERMINAL = {"completed", "pr_opened", "rejected", "failed"}
USAGE_COLUMNS = (
    "llm_calls",
    "tool_calls",
    "tool_failures",
    "unauthorized_attempts",
    "prompt_tokens",
    "completion_tokens",
    "cost_usd",
)


def _usage_sets(usage: dict) -> tuple[list[str], list[Any]]:
    keys = [k for k in USAGE_COLUMNS if k in usage]
    return [f"{k} = %s" for k in keys], [usage[k] for k in keys]


class PgRecorder:
    """One connection per recorder; a lock serialises its use, so a transaction from one task
    never interleaves with statements from another task on the same connection."""

    def __init__(self, conninfo: str):
        self._conninfo = conninfo
        self._conn: psycopg.AsyncConnection | None = None
        self._lock = anyio.Lock()

    @asynccontextmanager
    async def _use(self) -> AsyncIterator[psycopg.AsyncConnection]:
        async with self._lock:
            if self._conn is None or self._conn.closed:
                self._conn = await psycopg.AsyncConnection.connect(self._conninfo, autocommit=True)
            yield self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()

    async def create_run(
        self,
        run_id: str,
        *,
        repo: str,
        issue_text: str,
        task_id: str | None,
        issue_ref: str | None,
        model: str,
        task_mode: str,
        sandbox_id: str | None = None,
    ) -> None:
        async with self._use() as conn:
            await conn.execute(
                "INSERT INTO runs (id, repo, issue_text, task_id, issue_ref, model, task_mode,"
                " sandbox_id, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'queued')"
                # The API creates the row before the worker prepares the sandbox.
                " ON CONFLICT (id) DO UPDATE SET sandbox_id = EXCLUDED.sandbox_id,"
                " model = EXCLUDED.model, issue_text = EXCLUDED.issue_text",
                (run_id, repo, issue_text, task_id, issue_ref, model, task_mode, sandbox_id),
            )

    async def get_run(self, run_id: str) -> dict | None:
        async with self._use() as conn:
            cur = await conn.execute(
                "SELECT status, model, task_id, task_mode, sandbox_id, issue_ref, repo"
                " FROM runs WHERE id::text = %s",
                (run_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        keys = ("status", "model", "task_id", "task_mode", "sandbox_id", "issue_ref", "repo")
        return dict(zip(keys, row, strict=True))

    async def claim_for_approval(self, run_id: str) -> dict | None:
        """Atomically move a run from awaiting_approval to running. Exactly one concurrent
        caller gets the run's details; everyone else gets None and must not resume it."""
        async with self._use() as conn:
            cur = await conn.execute(
                "UPDATE runs SET status = 'running' WHERE id::text = %s"
                " AND status = 'awaiting_approval' RETURNING model, task_id, sandbox_id",
                (run_id,),
            )
            row = await cur.fetchone()
        return None if row is None else {"model": row[0], "task_id": row[1], "sandbox_id": row[2]}

    async def status(self, run_id: str, status: str, **fields: Any) -> None:
        sets, params = ["status = %s"], [status]
        for key in ("failure_reason", "pr_url", "retries", "trace_id", "error"):
            if key in fields:
                sets.append(f"{key} = %s")
                params.append(fields[key])
        for key in ("approval_request", "result"):
            if key in fields:
                sets.append(f"{key} = %s")
                params.append(Jsonb(fields[key]))
        usage_sets, usage_params = _usage_sets(fields.get("usage") or {})
        sets += usage_sets
        params += usage_params
        if status in TERMINAL:
            sets.append("finished_at = now()")
        async with self._use() as conn:
            await conn.execute(
                f"UPDATE runs SET {', '.join(sets)} WHERE id = %s", [*params, run_id]
            )

    async def event(self, run_id: str, node: str, kind: str, payload: dict) -> None:
        async with self._use() as conn, conn.transaction():
            # Lock the run row so writers on other connections cannot take the same seq.
            await conn.execute("SELECT 1 FROM runs WHERE id = %s FOR UPDATE", (run_id,))
            await conn.execute(
                "INSERT INTO run_events (run_id, seq, node, kind, payload)"
                " SELECT %s, COALESCE(MAX(seq), 0) + 1, %s, %s, %s FROM run_events"
                " WHERE run_id = %s",
                (run_id, node, kind, Jsonb(payload), run_id),
            )

    async def usage(self, run_id: str, usage: dict) -> None:
        """Live counters: the status (and finished_at) are left alone."""
        sets, params = _usage_sets(usage)
        if not sets:
            return
        async with self._use() as conn:
            await conn.execute(
                f"UPDATE runs SET {', '.join(sets)} WHERE id = %s", [*params, run_id]
            )

    async def tool_call(
        self, run_id: str, step: int, call: ToolCall, outcome: ToolOutcome, *, refused: bool = False
    ) -> None:
        try:
            args = json.loads(call.arguments or "{}")
        except json.JSONDecodeError:
            args = {"raw": call.arguments}
        async with self._use() as conn:
            await conn.execute(
                "INSERT INTO tool_calls (run_id, step, tool, args, ok, denied, refused,"
                " result_excerpt, error, latency_ms)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    run_id,
                    step,
                    call.name,
                    Jsonb(args),
                    outcome.ok,
                    outcome.denied,
                    refused,
                    outcome.content[:2000],
                    outcome.error,
                    outcome.latency_ms,
                ),
            )

    async def approval(
        self, run_id: str, *, revision: int, decision: str, comment: str | None, by: str
    ) -> None:
        async with self._use() as conn:
            await conn.execute(
                "INSERT INTO approvals (run_id, revision, decision, comment, decided_by)"
                " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (run_id, revision) DO NOTHING",
                (run_id, revision, decision, comment, by),
            )
