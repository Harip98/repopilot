"""Executes queued runs. Jobs are idempotent: a start job for a run that already finished does
nothing, and a redelivered start job continues from the run's last checkpoint."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import anyio
from langgraph.checkpoint.base import BaseCheckpointSaver

from repopilot.agent.deps import RunDeps
from repopilot.agent.runner import (
    RunResult,
    continue_run,
    finish_bench,
    has_checkpoint,
    prepare_bench_run,
    resume_run,
    start_run,
)
from repopilot.bench.tasks import Task
from repopilot.queue import Delivery, JobQueue

log = logging.getLogger(__name__)
TERMINAL = {"completed", "pr_opened", "rejected", "failed", "cancelled"}


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
            raise RuntimeError("GitHub runs are not configured")
        state = await svc.prepare_github(run, deps)
    else:
        task = svc.load_task(run["task_id"])
        if task is None:
            raise RuntimeError(f"unknown task {run['task_id']}")
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
    deps = svc.deps_for(run.get("model"))
    if job["type"] == "start":
        if run["status"] in TERMINAL or run["status"] == "awaiting_approval":
            return  # already done or waiting for a human: nothing to do
        result = await _start(run_id, run | {"auto_approve": job.get("auto_approve", False)},
                              svc, deps)
    elif job["type"] == "resume":
        result = await resume_run(run_id, job["decision"], deps, svc.checkpointer)
    else:
        raise ValueError(f"unknown job type {job['type']!r}")
    task = svc.load_task(run["task_id"]) if run.get("task_id") else None
    await finish_bench(result, task, deps)


class Worker:
    def __init__(self, queue: JobQueue, handle: Callable[[dict], Awaitable[None]], consumer: str,
                 *, visibility_ms: int = 1_800_000, max_deliveries: int = 3,
                 on_exhausted: Callable[[dict], Awaitable[None]] | None = None):
        self.queue, self.handle, self.consumer = queue, handle, consumer
        self.visibility_ms, self.max_deliveries = visibility_ms, max_deliveries
        self.on_exhausted = on_exhausted

    async def _process(self, delivery: Delivery) -> None:
        if delivery.deliveries > self.max_deliveries:
            log.error("job %s gave up after %d deliveries", delivery.id, delivery.deliveries - 1)
            if self.on_exhausted:
                await self.on_exhausted(delivery.job)
            await self.queue.ack(delivery.id)
            return
        try:
            await self.handle(delivery.job)
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
            await self.run_once()
