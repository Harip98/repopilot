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


def services(sandbox, recorder, saver, script, pr=None) -> WorkerServices:
    def deps_for(model):
        return RunDeps(
            llm=FakeChatModel(script),
            sandbox=sandbox,
            registry=build_registry(),
            recorder=recorder,
            pr=pr or NoPR(),
            clock=lambda: 1.0,
        )

    return WorkerServices(
        deps_for=deps_for, checkpointer=saver, recorder=recorder, load_task=lambda task_id: None
    )


async def _next(saver, run_id="run-1") -> tuple:
    from repopilot.agent.runner import pending_nodes

    return await pending_nodes(saver, run_id)


async def _pause(sandbox, recorder, saver) -> None:
    """Run run-1 up to its review, as a start job leaves it."""
    await start_run(
        make_state(), services(sandbox, recorder, saver, explore_and_edit()).deps_for("m"), saver
    )


class ResetPR:
    """Dies the way a worker does when its connection to GitHub drops mid-request."""

    async def open(self, state):
        raise ConnectionError("connection reset by peer")


class RecordingPR:
    def __init__(self):
        self.opened = []

    async def open(self, state):
        self.opened.append(state.run_id)
        return "https://github.com/acme/shop/pull/8"


class ForbiddenPR:
    async def open(self, state):
        from repopilot.github.client import GitHubError

        raise GitHubError(403, "Resource not accessible by personal access token")


APPROVE = {"type": "resume", "run_id": "run-1", "decision": {"decision": "approve", "by": "web"}}


async def test_resume_that_died_after_the_approval_is_continued_on_redelivery():
    saver, sandbox = InMemorySaver(), ScriptedSandbox({"app/store.py": STORE})
    recorder = RunRows(rows={"run-1": _row()})
    await _pause(sandbox, recorder, saver)
    with pytest.raises(ConnectionError):  # left unacknowledged: the job is redelivered
        await handle_job(APPROVE, services(sandbox, recorder, saver, [], ResetPR()))
    assert await _next(saver) == ("open_pr",)  # the decision itself was applied

    pr = RecordingPR()
    await handle_job(APPROVE, services(sandbox, recorder, saver, [], pr))  # the redelivery
    assert pr.opened == ["run-1"]
    assert recorder.statuses[-1][0] == "pr_opened" and await _next(saver) == ()
    assert sandbox.destroyed == ["0123456789abcdef"]


async def test_revision_that_died_is_continued_on_redelivery():
    saver, sandbox = InMemorySaver(), ScriptedSandbox({"app/store.py": STORE})
    recorder = RunRows(rows={"run-1": _row()})
    await _pause(sandbox, recorder, saver)
    job = {
        "type": "resume",
        "run_id": "run-1",
        "decision": {"decision": "reject", "comment": "use casefold", "by": "web"},
    }
    # One reply into the revision the model runs out: the worker dies mid-revision.
    with pytest.raises(AssertionError):
        await handle_job(
            job, services(sandbox, recorder, saver, [tool_reply("read_file", path="app/store.py")])
        )
    assert await _next(saver) == ("llm",)

    await handle_job(job, services(sandbox, recorder, saver, [tool_reply("finish", summary="x")]))
    assert recorder.statuses[-1][0] == "awaiting_approval"  # the revision is up for review
    assert await _next(saver) == ("approval",)
    assert sandbox.destroyed == []  # kept for the reviewer


async def test_github_403_when_opening_the_pr_fails_the_run_with_the_reason():
    saver, sandbox = InMemorySaver(), ScriptedSandbox({"app/store.py": STORE})
    recorder = RunRows(rows={"run-1": _row(task_mode="github")})
    await _pause(sandbox, recorder, saver)
    await handle_job(APPROVE, services(sandbox, recorder, saver, [], ForbiddenPR()))  # no raise
    status, fields = recorder.statuses[-1]
    assert status == "failed" and fields["failure_reason"] == "job_error"
    assert "Resource not accessible by personal access token" in fields["error"]
    assert sandbox.destroyed == ["0123456789abcdef"]


async def test_stale_resume_for_a_run_back_at_review_is_ignored():
    # The row is awaiting_approval: nobody claimed a decision, so this job's one is stale.
    saver, sandbox = InMemorySaver(), ScriptedSandbox({"app/store.py": STORE})
    recorder = RunRows(rows={"run-1": _row(status="awaiting_approval")})
    await _pause(sandbox, recorder, saver)
    seen = len(recorder.statuses)
    await handle_job(APPROVE, services(sandbox, recorder, saver, []))
    assert len(recorder.statuses) == seen and await _next(saver) == ("approval",)


def _github_services(sandbox, recorder, files: dict[str, bytes]) -> WorkerServices:
    """Worker services whose GitHub runs import a fake repository holding `files`."""
    import httpx

    from repopilot.github import GitHubClient, prepare_github_run
    from tests.github.test_github import TOKEN, GitHubFake, _dir, _file, _tarball

    root = "acme-shop-aaaaaaa/"
    archive = _tarball([_dir(root)] + [_file(root + p, b) for p, b in files.items()])
    github = GitHubClient(
        TOKEN, "https://api.github.test", transport=httpx.MockTransport(GitHubFake(archive))
    )

    async def prepare_github(run, deps, on_workspace=None):
        return await prepare_github_run(
            run, run_id=run["run_id"], github=github, sandbox=sandbox, on_workspace=on_workspace
        )

    svc = services(sandbox, recorder, InMemorySaver(), explore_and_edit())
    svc.prepare_github = prepare_github
    return svc


def _queued_github_row() -> dict:
    # As POST /v1/runs leaves it: queued, no sandbox yet.
    return _row(status="queued", task_mode="github", issue_ref="acme/shop#7", sandbox_id=None)


async def test_repo_config_with_a_command_the_sandbox_refuses_fails_the_queued_run_at_once():
    sandbox = ScriptedSandbox()
    recorder = RunRows(rows={"run-1": _queued_github_row()})
    svc = _github_services(sandbox, recorder, {".repopilot.yml": b"test_command: [npm, test]\n"})
    await handle_job({"type": "start", "run_id": "run-1"}, svc)  # must not raise: no retries
    status, fields = recorder.statuses[-1]
    assert status == "failed" and fields["failure_reason"] == "job_error"
    assert "test_command" in fields["error"] and "'npm'" in fields["error"]
    assert sandbox.execs == []  # refused before any sandbox was started


async def test_sandbox_refusal_while_preparing_fails_the_run_at_once():
    from repopilot.sandbox_client import SandboxError

    class Refusing(ScriptedSandbox):
        async def create(self, image):
            raise SandboxError("image_not_allowed", f"{image!r}", 400)

    recorder = RunRows(rows={"run-1": _queued_github_row()})
    svc = _github_services(Refusing(), recorder, {"app/main.py": b"x = 1\n"})
    await handle_job({"type": "start", "run_id": "run-1"}, svc)
    status, fields = recorder.statuses[-1]
    assert status == "failed" and "image_not_allowed" in fields["error"]


class FakeIndex:
    def __init__(self):
        self.built, self.queries, self.dropped = [], [], 0

    async def build(self, workspace):
        self.built.append(sorted(p.name for p in workspace.iterdir()))

    async def search(self, query):
        from repopilot.code_index import Hit

        self.queries.append(query)
        return [Hit("app/store.py", 1, 2, "def matches(status, wanted):", 0.03)]

    async def drop(self):
        self.dropped += 1


async def test_code_index_setting_gives_worker_runs_find_code_until_they_finish():
    from repopilot.bench.tasks import load_tasks

    task = next(t for t in load_tasks() if t.id == "orders-api-005")
    saver, sandbox = InMemorySaver(), ScriptedSandbox({"app/store.py": STORE})
    recorder = RunRows(rows={"run-1": _row(status="queued", task_id=task.id)})
    script = [tool_reply("find_code", query="where is the status compared?"), *explore_and_edit()]
    index = FakeIndex()

    def svc(script):
        s = services(sandbox, recorder, saver, script)
        deps_for = s.deps_for

        def with_find_code(model):
            deps = deps_for(model)
            deps.registry = build_registry(find_code_tool=True)
            return deps

        s.deps_for, s.load_task, s.code_index = with_find_code, {task.id: task}.get, lambda r: index
        return s

    await handle_job({"type": "start", "run_id": "run-1"}, svc(script))
    assert index.built and "app" in index.built[0]  # indexed from the host copy
    assert index.queries == ["where is the status compared?"]
    assert recorder.tool_calls[0].tool == "find_code" and recorder.tool_calls[0].ok
    assert index.dropped == 0  # kept while the run waits for its reviewer
    recorder.rows["run-1"]["status"] = "running"  # claimed by the API
    await handle_job(APPROVE, svc([]))
    assert recorder.statuses[-1][0] == "completed" and index.dropped == 1


@pytest.mark.parametrize(
    ("exc", "permanent"),
    [
        ("github-403", True),
        ("github-403-rate", False),
        ("github-429", False),
        ("github-422", True),
        ("github-500", False),
        ("sandbox-400", True),
        ("sandbox-413", True),
        ("sandbox-404", True),
        ("sandbox-409", False),
        ("sandbox-500", False),
    ],
)
def test_errors_that_cannot_succeed_on_retry_are_permanent(exc, permanent):
    from repopilot.github.client import GitHubError
    from repopilot.sandbox_client import SandboxError
    from repopilot.worker import _permanent

    kind, status, *rate = exc.split("-")
    if kind == "github":
        error = GitHubError(
            int(status),
            "API rate limit exceeded" if rate else "nope",
            rate_limited=bool(rate) or status == "429",
        )
    else:
        error = SandboxError("command_not_allowed", "x", int(status))
    assert _permanent(error) is permanent


async def test_start_job_idempotent():
    recorder = RunRows(
        rows={
            "run-1": {
                "status": "completed",
                "model": "m",
                "task_id": None,
                "task_mode": "bench",
                "sandbox_id": "s",
            }
        }
    )
    svc = services(ScriptedSandbox(), recorder, InMemorySaver(), script=[])  # any LLM call fails
    await handle_job({"type": "start", "run_id": "run-1"}, svc)
    assert recorder.statuses == []


async def test_reclaimed_start_continues_from_checkpoint():
    saver, sandbox = InMemorySaver(), ScriptedSandbox({"app/store.py": STORE})
    recorder = RunRows(
        rows={
            "run-1": {
                "status": "running",
                "model": "m",
                "task_id": None,
                "task_mode": "bench",
                "sandbox_id": "0123456789abcdef",
            }
        }
    )
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
    recorder = RunRows(
        rows={
            "run-1": {
                "status": "running",
                "model": "m",
                "task_id": None,
                "task_mode": "bench",
                "sandbox_id": "0123456789abcdef",
            }
        }
    )
    paused = await start_run(
        make_state(), services(sandbox, recorder, saver, explore_and_edit()).deps_for("m"), saver
    )
    assert paused.interrupt is not None
    await handle_job(
        {"type": "resume", "run_id": "run-1", "decision": {"decision": "approve", "by": "web"}},
        services(sandbox, recorder, saver, []),
    )
    assert recorder.statuses[-1][0] == "completed"
    assert sandbox.destroyed == ["0123456789abcdef"]


class ExpiredSandbox(ScriptedSandbox):
    async def exec(self, sandbox_id, argv, timeout_s=120, cwd=None):
        from repopilot.sandbox_client import SandboxError

        raise SandboxError("not_found", "no such sandbox", 404)


def _row(status="running", **kw):
    return {
        "status": status,
        "model": "m",
        "task_id": None,
        "task_mode": "bench",
        "sandbox_id": "0123456789abcdef",
    } | kw


async def test_duplicate_resume_after_the_run_finished_is_a_no_op():
    saver, sandbox = InMemorySaver(), ScriptedSandbox({"app/store.py": STORE})
    recorder = RunRows(rows={"run-1": _row()})
    await start_run(
        make_state(), services(sandbox, recorder, saver, explore_and_edit()).deps_for("m"), saver
    )
    job = {"type": "resume", "run_id": "run-1", "decision": {"decision": "approve", "by": "web"}}
    await handle_job(job, services(sandbox, recorder, saver, []))
    recorder.rows["run-1"]["status"] = "completed"
    seen = len(recorder.statuses)
    await handle_job(job, services(sandbox, recorder, saver, []))  # redelivered or duplicated
    assert len(recorder.statuses) == seen


async def test_resume_of_a_run_not_at_approval_does_not_fail_it():
    saver, sandbox = InMemorySaver(), ScriptedSandbox({"app/store.py": STORE})
    recorder = RunRows(rows={"run-1": _row()})
    await start_run(
        make_state(auto_approve=True),
        services(sandbox, recorder, saver, explore_and_edit()).deps_for("m"),
        saver,
    )
    recorder.rows["run-1"]["status"] = "running"  # row lagging behind the checkpoint
    seen = len(recorder.statuses)
    await handle_job(
        {"type": "resume", "run_id": "run-1", "decision": {"decision": "approve", "by": "web"}},
        services(sandbox, recorder, saver, []),
    )
    assert len(recorder.statuses) == seen


async def test_resume_with_expired_sandbox_fails_the_run_once():
    saver = InMemorySaver()
    recorder = RunRows(rows={"run-1": _row()})
    await start_run(
        make_state(),
        services(
            ScriptedSandbox({"app/store.py": STORE}), recorder, saver, explore_and_edit()
        ).deps_for("m"),
        saver,
    )
    await handle_job(
        {"type": "resume", "run_id": "run-1", "decision": {"decision": "approve", "by": "web"}},
        services(ExpiredSandbox(), recorder, saver, []),
    )
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
