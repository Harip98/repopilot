"""Run the agent over benchmark tasks with no human in the loop and record one result each.

Results go to a store keyed by (eval name, task id), so an interrupted eval resumes where it
stopped. A task whose harness raises, or whose run died on the LLM endpoint or the sandbox, is
recorded as status "error" (not a model failure) and is retried when the eval is resumed. A
resume must use the configuration the eval started with: the store keeps its meta.
"""

import hashlib
import json
import platform
import subprocess
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import psycopg
from psycopg.types.json import Jsonb

from repopilot.agent.runner import BenchOutcome, new_run_id
from repopilot.bench.freeze import task_hash
from repopilot.bench.tasks import Task
from repopilot.config import REPO_ROOT
from repopilot.eval.score import EvalResult, render_markdown, summarize

REPORTS_DIR = REPO_ROOT / "eval" / "reports"
RunTask = Callable[[Task, str], Awaitable[BenchOutcome]]
# Failures of the endpoint or the sandbox, not of the model.
INFRA_FAILURES = ("llm_error", "sandbox_error")
# Meta fields a resumed eval must share with the run that started it.
CONFIG_KEYS = ("model", "endpoint", "git_sha", "budget", "split", "task_hash")


class EvalConfigMismatch(RuntimeError):
    pass


class ResultStore(Protocol):
    async def done_task_ids(self, name: str) -> set[str]: ...

    async def get_meta(self, name: str) -> dict | None: ...

    async def put_meta(self, name: str, meta: dict) -> None: ...

    async def save(self, result: EvalResult) -> None: ...

    async def load(self, name: str) -> list[EvalResult]: ...


class MemoryResultStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], EvalResult] = {}
        self.metas: dict[str, dict] = {}

    async def done_task_ids(self, name: str) -> set[str]:
        return {task for (eval_run, task), r in self.rows.items()
                if eval_run == name and r.status != "error"}

    async def get_meta(self, name: str) -> dict | None:
        return self.metas.get(name)

    async def put_meta(self, name: str, meta: dict) -> None:
        self.metas[name] = dict(meta)

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
            cur = await conn.execute("SELECT task_id FROM eval_results"
                                     " WHERE eval_run = %s AND status <> 'error'", (name,))
            return {row[0] for row in await cur.fetchall()}

    async def get_meta(self, name: str) -> dict | None:
        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cur = await conn.execute("SELECT meta FROM eval_runs WHERE name = %s", (name,))
            row = await cur.fetchone()
            return row[0] if row else None

    async def put_meta(self, name: str, meta: dict) -> None:
        async with await psycopg.AsyncConnection.connect(self._dsn, autocommit=True) as conn:
            await conn.execute("INSERT INTO eval_runs (name, meta) VALUES (%s, %s)"
                               " ON CONFLICT (name) DO NOTHING", (name, Jsonb(meta)))

    async def save(self, result: EvalResult) -> None:
        row = result.model_dump()
        cols = [c for c in _COLUMNS if c != "run_id"]
        async with await psycopg.AsyncConnection.connect(self._dsn, autocommit=True) as conn:
            # run_id only when the run row exists: a harness error can precede its creation.
            await conn.execute(
                f"INSERT INTO eval_results ({', '.join(cols)}, run_id)"
                f" VALUES ({', '.join(['%s'] * len(cols))},"
                " (SELECT id FROM runs WHERE id::text = %s))"
                # A retried error row is replaced by the new attempt.
                " ON CONFLICT (eval_run, task_id) DO UPDATE SET "
                + ", ".join(f"{c} = EXCLUDED.{c}" for c in [*cols, "run_id"]),
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
    infra = (state.failure_reason or "").startswith(INFRA_FAILURES)
    return EvalResult(
        eval_run=name, task_id=task.id, run_id=run_id, model=model, difficulty=task.difficulty,
        split=task.split, status="error" if infra else state.status,
        failure_reason=state.failure_reason,
        resolved=state.status in ("completed", "pr_opened") and graded is not None and graded.resolved,
        patch_applied=bool(outcome.final_diff.strip()),
        visible_tests_passed=bool(state.last_tests and state.last_tests.passed),
        baseline_tests_passed=state.baseline_tests_passed, retries=state.retries,
        llm_calls=u.llm_calls, tool_calls=u.tool_calls, tool_failures=u.tool_failures,
        unauthorized_attempts=u.unauthorized_attempts,
        test_files_modified=bool(edited_existing_tests(outcome.final_diff)),
        latency_ms=latency_ms,
        prompt_tokens=u.prompt_tokens, completion_tokens=u.completion_tokens, cost_usd=u.cost_usd,
        heldout_exit_code=graded.exit_code if graded else None,
        error=state.failure_reason if infra else None,
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
                   clock: Callable[[], float] = time.monotonic,
                   meta: dict | None = None,
                   on_saved: Callable[[list[EvalResult]], None] | None = None,
                   ) -> list[EvalResult]:
    """`on_saved` gets every result so far after each task, e.g. to rewrite the report files
    so a killed eval still leaves a current report."""
    if meta is not None:
        started = await store.get_meta(name)
        if started is None:
            await store.put_meta(name, meta)
        else:
            changed = [k for k in CONFIG_KEYS if k in started and started[k] != meta.get(k)]
            if changed:
                raise EvalConfigMismatch(
                    f"eval {name!r} was started with a different configuration ("
                    + ", ".join(f"{k}: {started[k]!r} -> {meta.get(k)!r}" for k in changed)
                    + "); use a new --name")
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
        if on_saved:
            on_saved(await store.load(name))
    return await store.load(name)


def tasks_hash(tasks: list[Task]) -> str:
    """Short hash of exactly which tasks (and their fixture repos) an eval ran over."""
    digest = hashlib.sha256()
    for task in sorted(tasks, key=lambda t: t.id):
        digest.update(f"{task.id}:{task_hash(task)}\n".encode())
    return digest.hexdigest()[:16]


def eval_meta(name: str, model: str, base_url: str, tasks: list[Task], budget: dict, *,
              split: str) -> dict:
    def git_sha() -> str:
        proc = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT,
                              capture_output=True, text=True, check=False)
        return proc.stdout.strip() or "unknown"

    return {
        "model": model, "endpoint": base_url, "split": split, "tasks": len(tasks),
        "task_hash": tasks_hash(tasks), "git_sha": git_sha(),
        "date": datetime.now(UTC).strftime("%Y-%m-%d"), "host": platform.platform(terse=True),
        "budget": ", ".join(f"{k}={v}" for k, v in budget.items()),
    }


def write_report(name: str, results: list[EvalResult], meta: dict,
                 out_dir: Path = REPORTS_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{name}.jsonl").write_text(
        "".join(r.model_dump_json() + "\n" for r in results), encoding="utf-8", newline="\n")
    (out_dir / f"{name}.meta.json").write_text(json.dumps(meta, indent=2) + "\n",
                                               encoding="utf-8", newline="\n")
    md = out_dir / f"{name}.md"
    md.write_text(render_markdown(name, meta, summarize(results), results), encoding="utf-8",
                  newline="\n")
    return md
