"""Start, resume and grade agent runs; shared by the CLI, the eval harness and the API."""

import asyncio
import logging
import sys
import tempfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from pydantic import BaseModel

from repopilot.agent.deps import RunDeps
from repopilot.agent.graph import build_graph
from repopilot.agent.state import Budget, RunState
from repopilot.bench.tasks import Task, image_for
from repopilot.bench.workspace import build_base_workspace
from repopilot.sandbox_client import SandboxClient

GRAPH = build_graph()
log = logging.getLogger(__name__)


class RunNotFound(LookupError):
    pass


class NotAwaitingApproval(RuntimeError):
    pass


@dataclass
class RunResult:
    state: RunState
    interrupt: dict | None  # the approval payload when the run is paused for a human


class GradeResult(BaseModel):
    resolved: bool
    exit_code: int
    output_tail: str


def new_run_id() -> str:
    return str(uuid.uuid4())


def run_async[T](fn: Callable[..., Awaitable[T]], *args: Any) -> T:
    """asyncio.run with an event loop psycopg's async mode supports on every platform."""
    if sys.platform == "win32":
        return asyncio.run(fn(*args), loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(fn(*args))


@asynccontextmanager
async def postgres_checkpointer(dsn: str) -> AsyncIterator[AsyncPostgresSaver]:
    """LangGraph owns its checkpoint tables; setup() creates or migrates them idempotently."""
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        await saver.setup()
        yield saver


def _config(run_id: str) -> dict:
    return {"configurable": {"thread_id": run_id}}


def _result(out: dict) -> RunResult:
    interrupts = out.pop("__interrupt__", None)
    return RunResult(state=RunState.model_validate(out),
                     interrupt=interrupts[0].value if interrupts else None)


async def prepare_bench_run(task: Task, sandbox: SandboxClient, *, run_id: str,
                            auto_approve: bool = False, budget: Budget | None = None) -> RunState:
    """Build the task's buggy workspace, start a sandbox from its image and upload it."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace = build_base_workspace(task, Path(tmp) / "ws")
        sandbox_id = await sandbox.create(image_for(task))
        try:
            await sandbox.upload_dir(sandbox_id, workspace)
            baseline = await sandbox.exec(sandbox_id, task.test_command, timeout_s=300)
        except BaseException:
            await sandbox.destroy(sandbox_id)
            raise
    return RunState(
        run_id=run_id, task_id=task.id, repo=task.repo, issue_title=task.title,
        issue_text=task.issue, sandbox_id=sandbox_id, test_command=task.test_command,
        protected_paths=task.protected_paths, auto_approve=auto_approve,
        budget=budget or Budget(), baseline_tests_passed=baseline.exit_code == 0,
    )


async def start_run(state: RunState, deps: RunDeps, checkpointer: BaseCheckpointSaver) -> RunResult:
    graph = GRAPH.compile(checkpointer=checkpointer)
    return _result(await graph.ainvoke(state, _config(state.run_id), context=deps))


async def resume_run(run_id: str, decision: dict, deps: RunDeps,
                     checkpointer: BaseCheckpointSaver) -> RunResult:
    graph = GRAPH.compile(checkpointer=checkpointer)
    snapshot = await graph.aget_state(_config(run_id))
    if not snapshot.values:
        raise RunNotFound(run_id)
    if "approval" not in snapshot.next:
        raise NotAwaitingApproval(f"run {run_id} is {snapshot.values.get('status')}, "
                                  "not awaiting approval")
    return _result(await graph.ainvoke(Command(resume=decision), _config(run_id), context=deps))


async def grade(task: Task, sandbox: SandboxClient, sandbox_id: str) -> GradeResult:
    """Copy the held-out tests (and pristine test fixtures) in, then run them. Only ever called
    after the agent has finished: the model never sees these files."""
    await sandbox.upload_dir(sandbox_id, task.heldout_dir)
    res = await sandbox.exec(sandbox_id, task.heldout_command, timeout_s=300)
    return GradeResult(resolved=res.exit_code == 0, exit_code=res.exit_code,
                       output_tail=(res.stdout + res.stderr)[-4000:])


@dataclass
class BenchOutcome:
    result: RunResult
    graded: GradeResult | None  # None unless the run finished as completed or pr_opened


async def run_bench_task(task: Task, deps: RunDeps, checkpointer: BaseCheckpointSaver, *,
                         run_id: str, auto_approve: bool, budget: Budget | None = None) -> BenchOutcome:
    """Prepare, run and (when finished) grade one task. The sandbox is always destroyed unless
    the run is paused for approval, and an unexpected error marks the run failed."""
    state: RunState | None = None
    result: RunResult | None = None
    try:
        state = await prepare_bench_run(task, deps.sandbox, run_id=run_id,
                                        auto_approve=auto_approve, budget=budget)
        await deps.recorder.create_run(run_id, repo=task.repo, issue_text=task.issue,
                                       task_id=task.id, issue_ref=None, model=deps.llm.model,
                                       task_mode="bench", sandbox_id=state.sandbox_id)
        result = await start_run(state, deps, checkpointer)
        graded = None
        if result.interrupt is None and result.state.status in ("completed", "pr_opened"):
            graded = await grade(task, deps.sandbox, state.sandbox_id)
        return BenchOutcome(result=result, graded=graded)
    except Exception as exc:
        if state is not None:
            await deps.recorder.status(run_id, "failed", failure_reason="error",
                                       error=f"{type(exc).__name__}: {exc}"[:2000])
        raise
    finally:
        if state is not None and (result is None or result.interrupt is None):
            try:
                await deps.sandbox.destroy(state.sandbox_id)
            except Exception:
                log.warning("could not destroy sandbox %s", state.sandbox_id, exc_info=True)
