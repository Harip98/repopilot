"""Run the agent over benchmark tasks with no human in the loop and record one result each.

Results go to a store keyed by (eval name, task id), so an interrupted eval resumes where it
stopped. A task whose harness raises is recorded as status "error" and the eval moves on.
"""

import json
import platform
import subprocess
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import psycopg

from repopilot.agent.runner import BenchOutcome, new_run_id
from repopilot.bench.tasks import Task
from repopilot.config import REPO_ROOT
from repopilot.eval.score import EvalResult, render_markdown, summarize

REPORTS_DIR = REPO_ROOT / "eval" / "reports"
RunTask = Callable[[Task, str], Awaitable[BenchOutcome]]


class ResultStore(Protocol):
    async def done_task_ids(self, name: str) -> set[str]: ...

    async def save(self, result: EvalResult) -> None: ...

    async def load(self, name: str) -> list[EvalResult]: ...


class MemoryResultStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], EvalResult] = {}

    async def done_task_ids(self, name: str) -> set[str]:
        return {task for (eval_run, task) in self.rows if eval_run == name}

    async def save(self, result: EvalResult) -> None:
        self.rows[(result.eval_run, result.task_id)] = result

    async def load(self, name: str) -> list[EvalResult]:
        return [r for (eval_run, _), r in sorted(self.rows.items()) if eval_run == name]


_COLUMNS = list(EvalResult.model_fields)


class PgResultStore:
    def __init__(self, dsn: str):
        self._dsn = dsn

    async def done_task_ids(self, name: str) -> set[str]:
        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cur = await conn.execute("SELECT task_id FROM eval_results WHERE eval_run = %s", (name,))
            return {row[0] for row in await cur.fetchall()}

    async def save(self, result: EvalResult) -> None:
        row = result.model_dump()
        cols = [c for c in _COLUMNS if c != "run_id"]
        async with await psycopg.AsyncConnection.connect(self._dsn, autocommit=True) as conn:
            # run_id only when the run row exists: a harness error can precede its creation.
            await conn.execute(
                f"INSERT INTO eval_results ({', '.join(cols)}, run_id)"
                f" VALUES ({', '.join(['%s'] * len(cols))},"
                " (SELECT id FROM runs WHERE id::text = %s))",
                [row[c] for c in cols] + [row["run_id"]],
            )

    async def load(self, name: str) -> list[EvalResult]:
        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cur = await conn.execute(
                f"SELECT {', '.join(c for c in _COLUMNS if c != 'run_id')}, run_id::text"
                " FROM eval_results WHERE eval_run = %s ORDER BY task_id", (name,))
            names = [c for c in _COLUMNS if c != "run_id"] + ["run_id"]
            return [EvalResult(**dict(zip(names, row, strict=True))) for row in await cur.fetchall()]


def edited_existing_tests(diff: str) -> list[str]:
    """Test files under tests/ that the change modified or deleted (new test files are fine)."""
    edited, current, is_new = [], None, False
    for line in [*diff.splitlines(), "diff --git END END"]:
        if line.startswith("diff --git "):
            if current and current.startswith("tests/") and not is_new:
                edited.append(current)
            parts = line.split(" ")
            current = parts[2][2:] if parts[2].startswith("a/") else None
            is_new = False
        elif line.startswith("new file mode"):
            is_new = True
    return edited


def _from_outcome(name: str, task: Task, run_id: str, model: str, outcome: BenchOutcome,
                  latency_ms: int) -> EvalResult:
    state, graded = outcome.result.state, outcome.graded
    u = state.usage
    return EvalResult(
        eval_run=name, task_id=task.id, run_id=run_id, model=model, difficulty=task.difficulty,
        split=task.split, status=state.status, failure_reason=state.failure_reason,
        resolved=state.status in ("completed", "pr_opened") and graded is not None and graded.resolved,
        patch_applied=bool(state.diff.strip()),
        visible_tests_passed=bool(state.last_tests and state.last_tests.passed),
        baseline_tests_passed=state.baseline_tests_passed, retries=state.retries,
        llm_calls=u.llm_calls, tool_calls=u.tool_calls, tool_failures=u.tool_failures,
        unauthorized_attempts=u.unauthorized_attempts,
        test_files_modified=bool(edited_existing_tests(state.diff)), latency_ms=latency_ms,
        prompt_tokens=u.prompt_tokens, completion_tokens=u.completion_tokens, cost_usd=u.cost_usd,
        heldout_exit_code=graded.exit_code if graded else None, error=None,
    )


def _error_result(name: str, task: Task, run_id: str, model: str, exc: Exception,
                  latency_ms: int) -> EvalResult:
    return EvalResult(
        eval_run=name, task_id=task.id, run_id=run_id, model=model, difficulty=task.difficulty,
        split=task.split, status="error", failure_reason=None, resolved=False,
        patch_applied=False, visible_tests_passed=False, baseline_tests_passed=None, retries=0,
        llm_calls=0, tool_calls=0, tool_failures=0, unauthorized_attempts=0,
        test_files_modified=False, latency_ms=latency_ms, prompt_tokens=0, completion_tokens=0,
        cost_usd=0.0, heldout_exit_code=None, error=f"{type(exc).__name__}: {exc}"[:2000],
    )


async def run_eval(name: str, tasks: list[Task], run_task: RunTask, store: ResultStore, *,
                   model: str, on_result: Callable[[EvalResult], None] | None = None,
                   clock: Callable[[], float] = time.monotonic) -> list[EvalResult]:
    done = await store.done_task_ids(name)
    for task in tasks:
        if task.id in done:
            continue
        run_id, start = new_run_id(), clock()
        try:
            outcome = await run_task(task, run_id)
            result = _from_outcome(name, task, run_id, model, outcome,
                                   int((clock() - start) * 1000))
        except Exception as exc:  # noqa: BLE001 - one broken task must not end the eval
            result = _error_result(name, task, run_id, model, exc, int((clock() - start) * 1000))
        await store.save(result)
        if on_result:
            on_result(result)
    return await store.load(name)


def eval_meta(name: str, model: str, base_url: str, tasks: list[Task], budget: dict) -> dict:
    def git_sha() -> str:
        proc = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT,
                              capture_output=True, text=True, check=False)
        return proc.stdout.strip() or "unknown"

    return {
        "model": model, "endpoint": base_url, "tasks": len(tasks), "git_sha": git_sha(),
        "date": datetime.now(UTC).strftime("%Y-%m-%d"), "host": platform.platform(terse=True),
        "budget": ", ".join(f"{k}={v}" for k, v in budget.items()),
    }


def write_report(name: str, results: list[EvalResult], meta: dict,
                 out_dir: Path = REPORTS_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{name}.jsonl").write_text(
        "".join(r.model_dump_json() + "\n" for r in results), encoding="utf-8")
    (out_dir / f"{name}.meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    md = out_dir / f"{name}.md"
    md.write_text(render_markdown(name, meta, summarize(results), results), encoding="utf-8")
    return md
