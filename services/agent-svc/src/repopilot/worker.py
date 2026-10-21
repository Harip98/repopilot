"""Executes queued runs. Jobs are idempotent: a start job for a run that already finished does
nothing, a redelivered start job continues from the run's last checkpoint, and a resume job for
a run that is no longer waiting for its reviewer does nothing.

Errors that cannot succeed on a retry (a bad task, an unsafe archive, a GitHub 404) fail the run
at once with the error recorded; anything else is left unacknowledged for redelivery."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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
TERMINAL = {"completed", "pr_opened", "rejected", "failed", "cancelled"}


class JobError(RuntimeError):
    """A job that can never succeed as submitted."""


def _permanent(exc: Exception) -> bool:
    if isinstance(exc, GitHubError):
        return exc.status in (400, 401, 404, 410, 422)  # 403/429 may be rate limits
    return isinstance(exc, JobError | WorkspaceError | RunNotFound)


class RunStore(Protocol):
    async def get_run(self, run_id: str) -> dict | None: ...

    async def status(self, run_id: str, status: str, **fields: Any) -> None: ...


@dataclass
class WorkerServices:
    deps_for: Callable[[str | None], RunDeps]  # model name -> dependencies for one run
    checkpointer: BaseCheckpointSaver
    recorder: RunStore
    load_task: Callable[[str], Task | None]
    prepare_github: Callable[[dict, RunDeps], Awaitable[Any]] | None = None


async def _start(run_id: str, run: dict, svc: WorkerServices, deps: RunDeps) -> RunResult:
    if await has_checkpoint(svc.checkpointer, run_id):
        return await continue_run(run_id, deps, svc.checkpointer)  # a redelivered job
    if run["task_mode"] == "github":
        if svc.prepare_github is None:
            raise JobError("GitHub runs are not configured")
        state = await svc.prepare_github(run, deps)
    else:
        task = svc.load_task(run["task_id"])
        if task is None:
            raise JobError(f"unknown task {run['task_id']}")
        state = await prepare_bench_run(task, deps.sandbox, run_id=run_id,
                                        auto_approve=run.get("auto_approve", False))
    await deps.recorder.create_run(run_id, repo=state.repo, issue_text=state.issue_text,
                                   task_id=state.task_id, issue_ref=run.get("issue_ref"),
                                   model=deps.llm.model, task_mode=run["task_mode"],
                                   sandbox_id=state.sandbox_id)
    return await start_run(state, deps, svc.checkpointer)


async def handle_job(job: dict, svc: WorkerServices) -> None:
    run_id = job["run_id"]
    run = await svc.recorder.get_run(run_id)
    if run is None:
        log.warning("job for unknown run %s dropped", run_id)
        return
    if run["status"] in TERMINAL:
        return  # already finished: a redelivered or duplicate job
    deps = svc.deps_for(run.get("model"))
    try:
        if job["type"] == "start":
            if run["status"] == "awaiting_approval":
                return  # waiting for a human: nothing to do
            result = await _start(
                run_id, run | {"run_id": run_id, "auto_approve": job.get("auto_approve", False)},
                svc, deps)
        elif job["type"] == "resume":
            try:
                await deps.sandbox.exec(run["sandbox_id"], ["git", "--version"])
            except SandboxError as exc:
                if exc.status != 404:
                    raise
                # Reaped while the run waited for its reviewer: nothing left to resume.
                await svc.recorder.status(run_id, "failed", failure_reason="sandbox_expired")
                return
            try:
                result = await resume_run(run_id, job["decision"], deps, svc.checkpointer)
            except NotAwaitingApproval:
                log.warning("resume job for run %s dropped: not awaiting approval", run_id)
                return
        else:
            raise JobError(f"unknown job type {job['type']!r}")
        task = svc.load_task(run["task_id"]) if run.get("task_id") else None
        await finish_bench(result, task, deps)
    except Exception as exc:
        if not _permanent(exc):
            raise
        log.warning("job for run %s failed permanently: %s", run_id, exc)
        await svc.recorder.status(run_id, "failed", failure_reason="job_error",
                                  error=f"{type(exc).__name__}: {exc}"[:2000])


async def give_up(job: dict, recorder: RunStore) -> None:
    """A job that kept failing: fail its run, unless the run has finished meanwhile."""
    run = await recorder.get_run(job["run_id"])
    if run is not None and run["status"] not in TERMINAL:
        await recorder.status(job["run_id"], "failed", failure_reason="job_retries_exhausted")


class Worker:
    def __init__(self, queue: JobQueue, handle: Callable[[dict], Awaitable[None]], consumer: str,
                 *, visibility_ms: int = 1_800_000, max_deliveries: int = 3,
                 on_exhausted: Callable[[dict], Awaitable[None]] | None = None,
                 retry_s: float = 5.0):
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
