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
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.types import Command
from pydantic import BaseModel

from repopilot.agent.deps import RunDeps
from repopilot.agent.graph import build_graph
from repopilot.agent.nodes import workspace_changes
from repopilot.agent.state import Budget, RunState
from repopilot.bench.tasks import Task, image_for
from repopilot.bench.workspace import build_base_workspace
from repopilot.sandbox_client import SandboxClient
from repopilot.telemetry import run_span

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


def checkpoint_serde() -> JsonPlusSerializer:
    """A serializer that rebuilds only our own state models from checkpoints (LangGraph is
    moving to refusing unregistered types, which would break every resume)."""
    models = ("Plan", "Budget", "RunUsage", "TestReport", "Approval", "GitHubTarget", "RunState")
    return JsonPlusSerializer(allowed_msgpack_modules=[("repopilot.agent.state", m) for m in models])


@asynccontextmanager
async def postgres_checkpointer(dsn: str) -> AsyncIterator[AsyncPostgresSaver]:
    """LangGraph owns its checkpoint tables; setup() creates or migrates them idempotently."""
    async with AsyncPostgresSaver.from_conn_string(dsn, serde=checkpoint_serde()) as saver:
        await saver.setup()
        yield saver


def _config(run_id: str) -> dict:
    return {"configurable": {"thread_id": run_id}}


def _result(out: dict) -> RunResult:
    interrupts = out.pop("__interrupt__", None)
    return RunResult(state=RunState.model_validate(out),
                     interrupt=interrupts[0].value if interrupts else None)


WorkspaceHook = Callable[[Path], Awaitable[None]]


async def prepare_bench_run(task: Task, sandbox: SandboxClient, *, run_id: str,
                            auto_approve: bool = False, budget: Budget | None = None,
                            on_workspace: WorkspaceHook | None = None) -> RunState:
    """Build the task's buggy workspace, start a sandbox from its image and upload it.
    `on_workspace` sees the host copy first (e.g. to build the code index)."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace = build_base_workspace(task, Path(tmp) / "ws")
        if on_workspace is not None:
            await on_workspace(workspace)
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
    with run_span("repopilot.run", state.run_id, task_id=state.task_id, repo=state.repo,
                  model=deps.llm.model):
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
    with run_span("repopilot.run.resume", run_id, decision=decision.get("decision")):
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
    # The workspace's change at the end of the run, read before grading. state.diff is only
    # set by validation, so a run that edits and then stalls would otherwise look untouched.
    final_diff: str = ""


async def run_bench_task(task: Task, deps: RunDeps, checkpointer: BaseCheckpointSaver, *,
                         run_id: str, auto_approve: bool, budget: Budget | None = None,
                         on_workspace: WorkspaceHook | None = None) -> BenchOutcome:
    """Prepare, run and (when finished) grade one task. The sandbox is always destroyed unless
    the run is paused for approval, and an unexpected error marks the run failed."""
    state: RunState | None = None
    result: RunResult | None = None
    try:
        state = await prepare_bench_run(task, deps.sandbox, run_id=run_id,
                                        auto_approve=auto_approve, budget=budget,
                                        on_workspace=on_workspace)
        await deps.recorder.create_run(run_id, repo=task.repo, issue_text=task.issue,
                                       task_id=task.id, issue_ref=None, model=deps.llm.model,
                                       task_mode="bench", sandbox_id=state.sandbox_id)
        result = await start_run(state, deps, checkpointer)
        graded, final_diff = None, result.state.diff
        if result.interrupt is None:
            try:
                final_diff, _ = await workspace_changes(deps.sandbox, state.sandbox_id)
            except Exception:
                log.warning("could not read the final diff of %s", run_id, exc_info=True)
            if result.state.status in ("completed", "pr_opened"):
                graded = await grade(task, deps.sandbox, state.sandbox_id)
        return BenchOutcome(result=result, graded=graded, final_diff=final_diff)
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


async def has_checkpoint(checkpointer: BaseCheckpointSaver, run_id: str) -> bool:
    return await checkpointer.aget_tuple(_config(run_id)) is not None


async def continue_run(run_id: str, deps: RunDeps, checkpointer: BaseCheckpointSaver) -> RunResult:
    """Carry on a run from its last checkpoint (e.g. after its worker died)."""
    graph = GRAPH.compile(checkpointer=checkpointer)
    with run_span("repopilot.run.continue", run_id):
        return _result(await graph.ainvoke(None, _config(run_id), context=deps))


async def finish_bench(result: RunResult, task: Task | None, deps: RunDeps) -> GradeResult | None:
    """Once a run is over: grade benchmark runs, record the grade, and free the sandbox.
    A run paused for approval keeps its sandbox."""
    if result.interrupt is not None:
        return None
    sandbox_id = result.state.sandbox_id
    try:
        graded = None
        if task is not None and result.state.status in ("completed", "pr_opened"):
            graded = await grade(task, deps.sandbox, sandbox_id)
            await deps.recorder.event(result.state.run_id, "grade", "heldout",
                                      graded.model_dump(exclude={"output_tail"}))
        return graded
    finally:
        try:
            await deps.sandbox.destroy(sandbox_id)
        except Exception:
            log.warning("could not destroy sandbox %s", sandbox_id, exc_info=True)
