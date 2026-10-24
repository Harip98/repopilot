"""Executes queued runs. Jobs are idempotent: a start job for a run that already finished does
nothing, and a redelivered start job continues from the run's last checkpoint. A resume job
applies the reviewer's decision; redelivered after the decision was applied (its worker died
opening the PR or during a revision), it continues from the last checkpoint too. A resume job
for a run that finished, or that is back at review without a new decision, does nothing.

Errors that cannot succeed on a retry (a bad task or repo config, an unsafe archive, a GitHub
token without the permission, a command or file the sandbox refuses) fail the run at once with
the error recorded; anything else is left unacknowledged for redelivery."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import anyio
from langgraph.checkpoint.base import BaseCheckpointSaver

from repopilot.agent.deps import RunDeps
from repopilot.agent.runner import (
    NotAwaitingApproval,
    RunNotFound,
    RunResult,
    continue_run,
    finish_bench,
    has_checkpoint,
    pending_nodes,
    prepare_bench_run,
    resume_run,
    start_run,
)
from repopilot.bench.tasks import Task
from repopilot.github.client import GitHubError
from repopilot.github.workspace import WorkspaceError
from repopilot.queue import Delivery, JobQueue
from repopilot.sandbox_client import SandboxError

log = logging.getLogger(__name__)
TERMINAL = {"completed", "pr_opened", "rejected", "failed"}


class JobError(RuntimeError):
    """A job that can never succeed as submitted."""


def _permanent(exc: Exception) -> bool:
    if isinstance(exc, GitHubError):
        # 403 is mostly a token without the permission ("Resource not accessible by personal
        # access token"); a rate limit, 403 or 429, passes once its window resets.
        return exc.status in (400, 401, 403, 404, 410, 422) and not exc.rate_limited
    if isinstance(exc, SandboxError):
        # A refused request (command, image, path or argv not allowed, file too large, no such
        # sandbox) is refused again; 409 sandbox_exhausted asks for a retry.
        return 400 <= exc.status < 500 and exc.status not in (408, 409, 429)
    return isinstance(exc, JobError | WorkspaceError | RunNotFound)


class RunStore(Protocol):
    async def get_run(self, run_id: str) -> dict | None: ...

    async def status(self, run_id: str, status: str, **fields: Any) -> None: ...


class RunIndex(Protocol):
    """One run's slice of the hybrid code index (repopilot.code_index.RunIndex)."""

    async def build(self, workspace: Path) -> None: ...

    async def search(self, query: str) -> list: ...

    async def drop(self) -> None: ...


@dataclass
class WorkerServices:
    deps_for: Callable[[str | None], RunDeps]  # model name -> dependencies for one run
    checkpointer: BaseCheckpointSaver
    recorder: RunStore
    load_task: Callable[[str], Task | None]
    # (run, deps, on_workspace=...) -> the prepared RunState of a GitHub-issue run
    prepare_github: Callable[..., Awaitable[Any]] | None = None
    # run id -> its code index; set when REPOPILOT_CODE_INDEX gives worker runs find_code
    code_index: Callable[[str], RunIndex] | None = None


async def _start(
    run_id: str, run: dict, svc: WorkerServices, deps: RunDeps, index: RunIndex | None
) -> RunResult:
    if await has_checkpoint(svc.checkpointer, run_id):
        return await continue_run(run_id, deps, svc.checkpointer)  # a redelivered job
    on_workspace = index.build if index is not None else None
    if run["task_mode"] == "github":
        if svc.prepare_github is None:
            raise JobError("GitHub runs are not configured")
        state = await svc.prepare_github(run, deps, on_workspace=on_workspace)
    else:
        task = svc.load_task(run["task_id"])
        if task is None:
            raise JobError(f"unknown task {run['task_id']}")
        state = await prepare_bench_run(
            task,
            deps.sandbox,
            run_id=run_id,
            auto_approve=run.get("auto_approve", False),
            on_workspace=on_workspace,
        )
    await deps.recorder.create_run(
        run_id,
        repo=state.repo,
        issue_text=state.issue_text,
        task_id=state.task_id,
        issue_ref=run.get("issue_ref"),
        model=deps.llm.model,
        task_mode=run["task_mode"],
        sandbox_id=state.sandbox_id,
    )
    return await start_run(state, deps, svc.checkpointer)


async def _resume(
    run_id: str, decision: dict, deps: RunDeps, checkpointer: BaseCheckpointSaver
) -> RunResult | None:
    try:
        return await resume_run(run_id, decision, deps, checkpointer)
    except NotAwaitingApproval:
        if await pending_nodes(checkpointer, run_id):
            # Past the approval but not finished: this job applied its decision, then its
            # worker died (opening the PR, or mid-revision). Nothing else will drive the run.
            log.warning("run %s resumed earlier and stopped; continuing it", run_id)
            return await continue_run(run_id, deps, checkpointer)
        log.warning("resume job for run %s dropped: not awaiting approval", run_id)
        return None


async def _release(run_id: str, svc: WorkerServices, deps: RunDeps, index: RunIndex | None) -> None:
    """Free what a permanently failed run holds. Best effort: the run is failed already."""
    try:
        if (run := await svc.recorder.get_run(run_id)) and run.get("sandbox_id"):
            await deps.sandbox.destroy(run["sandbox_id"])
    except Exception:
        log.warning("could not destroy the sandbox of %s", run_id, exc_info=True)
    await _drop_index(run_id, index)


async def _drop_index(run_id: str, index: RunIndex | None) -> None:
    if index is None:
        return
    try:
        await index.drop()
    except Exception:
        log.warning("could not drop the code index of %s", run_id, exc_info=True)


async def handle_job(job: dict, svc: WorkerServices) -> None:
    run_id = job["run_id"]
    run = await svc.recorder.get_run(run_id)
    if run is None:
        log.warning("job for unknown run %s dropped", run_id)
        return
    if run["status"] in TERMINAL:
        return  # already finished: a redelivered or duplicate job
    deps = svc.deps_for(run.get("model"))
    index = svc.code_index(run_id) if svc.code_index is not None else None
    if index is not None:
        deps.code_search = index.search
    try:
        if job["type"] == "start":
            if run["status"] == "awaiting_approval":
                return  # waiting for a human: nothing to do
            result = await _start(
                run_id,
                run | {"run_id": run_id, "auto_approve": job.get("auto_approve", False)},
                svc,
                deps,
                index,
            )
        elif job["type"] == "resume":
            if run["status"] == "awaiting_approval":
                # A decision is claimed (the row set running) before its job is queued, so
                # this one is stale: the run is back at review, waiting for a new decision.
                log.warning("resume job for run %s dropped: no claimed decision", run_id)
                return
            try:
                await deps.sandbox.exec(run["sandbox_id"], ["git", "--version"])
            except SandboxError as exc:
                if exc.status != 404:
                    raise
                # Reaped while the run waited for its reviewer: nothing left to resume.
                await svc.recorder.status(run_id, "failed", failure_reason="sandbox_expired")
                await _drop_index(run_id, index)
                return
            result = await _resume(run_id, job["decision"], deps, svc.checkpointer)
            if result is None:
                return
        else:
            raise JobError(f"unknown job type {job['type']!r}")
        task = svc.load_task(run["task_id"]) if run.get("task_id") else None
        await finish_bench(result, task, deps)
        if result.interrupt is None:
            await _drop_index(run_id, index)  # stale after the agent's edits
    except Exception as exc:
        if not _permanent(exc):
            raise
        log.warning("job for run %s failed permanently: %s", run_id, exc)
        await svc.recorder.status(
            run_id,
            "failed",
            failure_reason="job_error",
            error=f"{type(exc).__name__}: {exc}"[:2000],
        )
        await _release(run_id, svc, deps, index)


async def give_up(job: dict, recorder: RunStore) -> None:
    """A job that kept failing: fail its run, unless the run has finished meanwhile."""
    run = await recorder.get_run(job["run_id"])
    if run is not None and run["status"] not in TERMINAL:
        await recorder.status(job["run_id"], "failed", failure_reason="job_retries_exhausted")


class Worker:
    def __init__(
        self,
        queue: JobQueue,
        handle: Callable[[dict], Awaitable[None]],
        consumer: str,
        *,
        visibility_ms: int = 1_800_000,
        max_deliveries: int = 3,
        on_exhausted: Callable[[dict], Awaitable[None]] | None = None,
        retry_s: float = 5.0,
    ):
        self.queue, self.handle, self.consumer = queue, handle, consumer
        self.visibility_ms, self.max_deliveries = visibility_ms, max_deliveries
        self.on_exhausted, self.retry_s = on_exhausted, retry_s

    async def _keep_lease(self, entry_id: str) -> None:
        # A run can outlast the visibility timeout; renew well before it lapses so no other
        # worker reclaims a job that is still being handled.
        while True:
            await anyio.sleep(self.visibility_ms / 3000)
            try:
                await self.queue.touch(self.consumer, entry_id)
            except Exception:
                log.warning("could not renew the lease on %s", entry_id, exc_info=True)

    async def _handle_leased(self, delivery: Delivery) -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._keep_lease, delivery.id)
            try:
                await self.handle(delivery.job)
            finally:
                tg.cancel_scope.cancel()

    async def _process(self, delivery: Delivery) -> None:
        if delivery.deliveries > self.max_deliveries:
            log.error("job %s gave up after %d deliveries", delivery.id, delivery.deliveries - 1)
            if self.on_exhausted:
                await self.on_exhausted(delivery.job)
            await self.queue.ack(delivery.id)
            return
        try:
            await self._handle_leased(delivery)
        except Exception:
            # Not acknowledged: the entry is reclaimed after the visibility timeout.
            log.exception("job %s failed (delivery %d)", delivery.id, delivery.deliveries)
            return
        await self.queue.ack(delivery.id)

    async def run_once(self, block_ms: int = 5000) -> bool:
        for delivery in await self.queue.reclaim(self.consumer, self.visibility_ms):
            await self._process(delivery)
        delivery = await self.queue.next(self.consumer, block_ms)
        if delivery is None:
            return False
        await self._process(delivery)
        return True

    async def run_forever(self, stop: anyio.Event) -> None:
        await self.queue.ensure_group()
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:
                # Redis or Postgres blipped: the job (if any) stays pending and is reclaimed.
                log.exception("worker loop error; retrying in %.0fs", self.retry_s)
                await anyio.sleep(self.retry_s)
