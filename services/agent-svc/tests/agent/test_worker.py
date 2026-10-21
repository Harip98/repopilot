from dataclasses import dataclass, field

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from repopilot.agent.deps import RunDeps
from repopilot.agent.pr import NoPR
from repopilot.agent.recorder import MemoryRecorder
from repopilot.agent.runner import start_run
from repopilot.llm import FakeChatModel
from repopilot.tools.builtin import build_registry
from repopilot.worker import WorkerServices, give_up, handle_job
from tests.agent.test_graph import STORE, explore_and_edit, make_state
from tests.fakes import ScriptedSandbox, tool_reply

pytestmark = pytest.mark.anyio


@dataclass
class RunRows(MemoryRecorder):
    """MemoryRecorder plus the run rows the worker reads."""

    rows: dict[str, dict] = field(default_factory=dict)

    async def get_run(self, run_id: str) -> dict | None:
        return self.rows.get(run_id)


def services(sandbox, recorder, saver, script) -> WorkerServices:
    def deps_for(model):
        return RunDeps(llm=FakeChatModel(script), sandbox=sandbox, registry=build_registry(),
                       recorder=recorder, pr=NoPR(), clock=lambda: 1.0)

    return WorkerServices(deps_for=deps_for, checkpointer=saver, recorder=recorder,
                          load_task=lambda task_id: None)


async def test_start_job_idempotent():
    recorder = RunRows(rows={"run-1": {"status": "completed", "model": "m", "task_id": None,
                                       "task_mode": "bench", "sandbox_id": "s"}})
    svc = services(ScriptedSandbox(), recorder, InMemorySaver(), script=[])  # any LLM call fails
    await handle_job({"type": "start", "run_id": "run-1"}, svc)
    assert recorder.statuses == []


async def test_reclaimed_start_continues_from_checkpoint():
    saver, sandbox = InMemorySaver(), ScriptedSandbox({"app/store.py": STORE})
    recorder = RunRows(rows={"run-1": {"status": "running", "model": "m", "task_id": None,
                                       "task_mode": "bench", "sandbox_id": "0123456789abcdef"}})
    crashed = services(sandbox, recorder, saver, explore_and_edit()[:3])
    with pytest.raises(AssertionError):  # the first worker dies mid-run
        await start_run(make_state(auto_approve=True), crashed.deps_for("m"), saver)
    calls_before = len(recorder.tool_calls)

    svc = services(sandbox, recorder, saver, [tool_reply("finish", summary="done")])
    await handle_job({"type": "start", "run_id": "run-1"}, svc)
    assert recorder.statuses[-1][0] == "completed"
    assert len(recorder.tool_calls) == calls_before  # nothing re-run
    assert sandbox.destroyed == ["0123456789abcdef"]


async def test_resume_job_runs_resume():
    saver, sandbox = InMemorySaver(), ScriptedSandbox({"app/store.py": STORE})
    recorder = RunRows(rows={"run-1": {"status": "running", "model": "m", "task_id": None,
                                       "task_mode": "bench", "sandbox_id": "0123456789abcdef"}})
    paused = await start_run(make_state(), services(sandbox, recorder, saver,
                                                    explore_and_edit()).deps_for("m"), saver)
    assert paused.interrupt is not None
    await handle_job({"type": "resume", "run_id": "run-1",
                      "decision": {"decision": "approve", "by": "web"}},
                     services(sandbox, recorder, saver, []))
    assert recorder.statuses[-1][0] == "completed"
    assert sandbox.destroyed == ["0123456789abcdef"]


class ExpiredSandbox(ScriptedSandbox):
    async def exec(self, sandbox_id, argv, timeout_s=120, cwd=None):
        from repopilot.sandbox_client import SandboxError

        raise SandboxError("not_found", "no such sandbox", 404)


def _row(status="running", **kw):
    return {"status": status, "model": "m", "task_id": None, "task_mode": "bench",
            "sandbox_id": "0123456789abcdef"} | kw


async def test_duplicate_resume_after_the_run_finished_is_a_no_op():
    saver, sandbox = InMemorySaver(), ScriptedSandbox({"app/store.py": STORE})
    recorder = RunRows(rows={"run-1": _row()})
    await start_run(make_state(), services(sandbox, recorder, saver, explore_and_edit()).deps_for("m"),
                    saver)
    job = {"type": "resume", "run_id": "run-1", "decision": {"decision": "approve", "by": "web"}}
    await handle_job(job, services(sandbox, recorder, saver, []))
    recorder.rows["run-1"]["status"] = "completed"
    seen = len(recorder.statuses)
    await handle_job(job, services(sandbox, recorder, saver, []))  # redelivered or duplicated
    assert len(recorder.statuses) == seen


async def test_resume_of_a_run_not_at_approval_does_not_fail_it():
    saver, sandbox = InMemorySaver(), ScriptedSandbox({"app/store.py": STORE})
    recorder = RunRows(rows={"run-1": _row()})
    await start_run(make_state(auto_approve=True),
                    services(sandbox, recorder, saver, explore_and_edit()).deps_for("m"), saver)
    recorder.rows["run-1"]["status"] = "running"  # row lagging behind the checkpoint
    seen = len(recorder.statuses)
    await handle_job({"type": "resume", "run_id": "run-1",
                      "decision": {"decision": "approve", "by": "web"}},
                     services(sandbox, recorder, saver, []))
    assert len(recorder.statuses) == seen


async def test_resume_with_expired_sandbox_fails_the_run_once():
    saver = InMemorySaver()
    recorder = RunRows(rows={"run-1": _row()})
    await start_run(make_state(), services(ScriptedSandbox({"app/store.py": STORE}), recorder, saver,
                                           explore_and_edit()).deps_for("m"), saver)
    await handle_job({"type": "resume", "run_id": "run-1",
                      "decision": {"decision": "approve", "by": "web"}},
                     services(ExpiredSandbox(), recorder, saver, []))
    assert recorder.statuses[-1] == ("failed", {"failure_reason": "sandbox_expired"})


async def test_permanent_job_error_fails_the_run_without_retry():
    recorder = RunRows(rows={"run-1": _row(task_id="no-such-task-001")})
    svc = services(ScriptedSandbox(), recorder, InMemorySaver(), [])
    await handle_job({"type": "start", "run_id": "run-1"}, svc)  # must not raise
    status, fields = recorder.statuses[-1]
    assert status == "failed" and fields["failure_reason"] == "job_error"
    assert "no-such-task-001" in fields["error"]


async def test_transient_job_error_is_raised_for_redelivery():
    class Down(ScriptedSandbox):
        async def create(self, image):
            raise ConnectionError("sandbox-svc unreachable")

    from repopilot.bench.tasks import load_tasks

    task = next(t for t in load_tasks() if t.id == "orders-api-005")
    recorder = RunRows(rows={"run-1": _row(task_id=task.id)})
    svc = services(Down(), recorder, InMemorySaver(), [])
    svc.load_task = {task.id: task}.get
    with pytest.raises(ConnectionError):
        await handle_job({"type": "start", "run_id": "run-1"}, svc)


async def test_exhausted_job_does_not_overwrite_a_finished_run():
    recorder = RunRows(rows={"run-1": _row(status="completed"), "run-2": _row()})
    await give_up({"run_id": "run-1"}, recorder)
    await give_up({"run_id": "run-2"}, recorder)
    assert recorder.statuses == [("failed", {"failure_reason": "job_retries_exhausted"})]


async def test_worker_survives_queue_errors():
    import anyio

    from repopilot.worker import Worker

    class FlakyQueue:
        def __init__(self):
            self.calls, self.stop = 0, anyio.Event()

        async def ensure_group(self):
            pass

        async def reclaim(self, consumer, min_idle_ms):
            return []

        async def next(self, consumer, block_ms):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("redis restarting")
            self.stop.set()

    q = FlakyQueue()

    async def handle(job):
        raise AssertionError("no jobs")

    await Worker(q, handle, "w1", retry_s=0).run_forever(q.stop)
    assert q.calls == 2
