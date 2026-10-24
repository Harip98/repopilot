"""The CLI commands, with Postgres, the sandbox, the LLM and GitHub replaced by test doubles."""

from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import anyio
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from typer.testing import CliRunner

from repopilot import cli
from repopilot.agent.state import GitHubTarget
from repopilot.config import Settings
from repopilot.eval.runner import REPORTS_DIR, MemoryResultStore
from repopilot.llm import FakeChatModel
from tests.agent.test_graph import STORE, explore_and_edit, make_state
from tests.agent.test_worker import RunRows, services
from tests.fakes import ScriptedSandbox

RUN = "run-1"
GH = GitHubTarget(
    repo="acme/shop",
    issue_number=7,
    issue_url="https://github.com/acme/shop/issues/7",
    base_branch="main",
    base_sha="a" * 40,
)


class CliSandbox(ScriptedSandbox):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


@dataclass
class CliRecorder(RunRows):
    claims: list[str] = field(default_factory=list)

    async def claim_for_approval(self, run_id: str) -> dict | None:
        self.claims.append(run_id)
        row = self.rows.get(run_id)
        if row is None or row["status"] != "awaiting_approval":
            return None
        row["status"] = "running"
        return {"model": row["model"], "task_id": row["task_id"], "sandbox_id": row["sandbox_id"]}

    async def status(self, run_id: str, status: str, **fields) -> None:
        await super().status(run_id, status, **fields)
        if run_id in self.rows:
            self.rows[run_id]["status"] = status

    async def close(self) -> None:
        pass


class FakePR:
    def __init__(self, github, sandbox):
        self.github = github
        FakePR.opened.append(github)

    async def open(self, state):
        return f"https://github.com/{state.github.repo}/pull/8"


class FakeGitHub:
    def __init__(self, token, api_url):
        self.token, self.closed = token, False

    async def aclose(self):
        self.closed = True


@pytest.fixture
def env(monkeypatch):
    @dataclass
    class Env:
        saver: InMemorySaver = field(default_factory=InMemorySaver)
        sandbox: CliSandbox = field(default_factory=lambda: CliSandbox({"app/store.py": STORE}))
        recorder: CliRecorder = field(default_factory=CliRecorder)
        script: list = field(default_factory=list)
        settings: Settings = field(default_factory=lambda: Settings(_env_file=None))
        store: MemoryResultStore = field(default_factory=MemoryResultStore)

        def pause(self, **state_kw) -> None:
            """A run paused for review, as the worker or `solve` leaves it."""
            row = {
                "status": "awaiting_approval",
                "model": "m",
                "task_id": state_kw.get("task_id", "orders-api-005"),
                "sandbox_id": "0123456789abcdef",
                "task_mode": "github" if state_kw.get("github") else "bench",
            }
            self.recorder.rows[RUN] = row
            svc = services(self.sandbox, self.recorder, self.saver, explore_and_edit())
            anyio.run(cli_start, make_state(**state_kw), svc.deps_for("m"), self.saver)
            row["status"] = "awaiting_approval"

    e = Env()

    @asynccontextmanager
    async def checkpointer(dsn):
        yield e.saver

    FakePR.opened = []
    monkeypatch.setattr("repopilot.agent.pg_recorder.PgRecorder", lambda dsn: e.recorder)
    monkeypatch.setattr("repopilot.sandbox_client.get_sandbox_client", lambda: e.sandbox)
    monkeypatch.setattr("repopilot.agent.runner.postgres_checkpointer", checkpointer)
    monkeypatch.setattr("repopilot.llm.get_chat_model", lambda model=None: FakeChatModel(e.script))
    monkeypatch.setattr("repopilot.config.get_settings", lambda: e.settings)
    monkeypatch.setattr("repopilot.github.GitHubClient", FakeGitHub)
    monkeypatch.setattr("repopilot.github.GitHubPR", FakePR)
    monkeypatch.setattr("repopilot.eval.runner.PgResultStore", lambda dsn: e.store)
    monkeypatch.setattr(cli, "_run_index_if_built", _no_index)
    return e


async def _no_index(run_id):
    return None


async def cli_start(state, deps, saver):
    from repopilot.agent.runner import start_run

    await start_run(state, deps, saver)


def invoke(*args: str):
    return CliRunner().invoke(cli.app, list(args))


def test_approving_a_github_run_without_a_token_is_refused_before_the_claim(env):
    env.pause(task_id=None, github=GH)
    result = invoke("approve", RUN)
    assert result.exit_code == 1
    assert "REPOPILOT_GITHUB_TOKEN" in result.output and "awaiting approval" in result.output
    assert env.recorder.claims == [] and env.recorder.rows[RUN]["status"] == "awaiting_approval"
    assert env.sandbox.destroyed == []  # the change is still there for a later approval


def test_approving_a_github_run_opens_its_pull_request(env):
    env.settings = Settings(_env_file=None, github_token="ghp_test")
    env.pause(task_id=None, github=GH)
    result = invoke("approve", RUN, "--by", "alice")
    assert result.exit_code == 0, result.output
    assert len(FakePR.opened) == 1 and FakePR.opened[0].token == "ghp_test"
    assert FakePR.opened[0].closed
    status, fields = env.recorder.statuses[-1]
    assert status == "pr_opened" and fields["pr_url"] == "https://github.com/acme/shop/pull/8"


def test_approving_a_bench_run_records_its_heldout_grade(env):
    env.pause()
    result = invoke("approve", RUN)
    assert result.exit_code == 0, result.output
    assert "held-out tests:" in result.output and FakePR.opened == []
    assert env.recorder.statuses[-1][0] == "completed"
    node, kind, payload = env.recorder.events[-1]
    assert (node, kind) == ("grade", "heldout") and set(payload) == {"resolved", "exit_code"}
    assert env.sandbox.destroyed == ["0123456789abcdef"]


def test_a_revision_after_review_keeps_find_code(env, monkeypatch):
    from repopilot.code_index import Hit
    from tests.fakes import tool_reply

    class Index:
        dropped = 0

        async def search(self, query):
            return [Hit("app/store.py", 1, 2, "def matches(status, wanted):", 0.03)]

        async def drop(self):
            Index.dropped += 1

    async def built(run_id):
        return Index()

    monkeypatch.setattr(cli, "_run_index_if_built", built)
    env.pause()
    env.script = [
        tool_reply("find_code", query="status compare"),
        tool_reply("finish", summary="revised"),
    ]
    result = invoke("approve", RUN, "--reject", "--comment", "use casefold")
    assert result.exit_code == 0, result.output
    assert [c.tool for c in env.recorder.tool_calls][-1] == "find_code"
    assert env.recorder.tool_calls[-1].ok  # the revision still has the tool
    assert Index.dropped == 0 and env.sandbox.destroyed == []  # paused for review again


def test_solve_keeps_the_code_index_while_the_run_waits_for_review(env, monkeypatch):
    calls = []

    class Index:
        async def build(self, workspace):
            calls.append("build")

        async def search(self, query):
            return []

        async def drop(self):
            calls.append("drop")

    monkeypatch.setattr(
        cli, "_code_index_hooks", lambda enabled, run_id: Index() if enabled else None
    )
    env.script = explore_and_edit()
    result = invoke("solve", "--task", "orders-api-005", "--code-index")
    assert result.exit_code == 0, result.output
    assert "awaiting approval" in result.output and calls == ["build"]

    env.script = explore_and_edit()
    result = invoke("solve", "--task", "orders-api-005", "--code-index", "--auto-approve")
    assert result.exit_code == 0, result.output
    assert calls == ["build", "build", "drop"]


def test_eval_run_checks_the_freeze_of_selected_test_tasks(env, monkeypatch):
    from repopilot.bench.tasks import load_tasks

    [task, *_] = [t for t in load_tasks(split="test")]
    monkeypatch.setattr("repopilot.bench.freeze.task_hash", lambda t, repos_root=None: "0" * 64)
    result = invoke("eval", "run", "--name", "x", "--split", "test", "--task", task.id)
    assert result.exit_code == 1
    assert f"{task.id}: changed since freeze" in result.output
    assert "frozen but missing" not in result.output  # only the selected task is checked


def test_eval_run_refuses_unknown_task_ids(env):
    result = invoke("eval", "run", "--name", "x", "--split", "dev", "--task", "nope-001")
    assert result.exit_code == 1 and "nope-001" in result.output


def test_eval_import_loads_the_shipped_reports(env):
    result = invoke("eval", "import")
    assert result.exit_code == 0, result.output
    for name in ("qwen3b-test", "qwen7b-dev", "qwen7b-dev2", "qwen7b-test"):
        assert f"{name}\timported" in result.output
    assert len(anyio.run(env.store.load, "qwen7b-dev")) == 15
    again = invoke("eval", "import", str(REPORTS_DIR))
    assert again.exit_code == 0 and again.output.count("skipped") == 4
