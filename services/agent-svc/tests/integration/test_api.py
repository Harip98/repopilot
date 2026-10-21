"""The HTTP API against the real test database; the job queue is a fake."""

import uuid

import anyio
import httpx
import psycopg
import pytest
from psycopg.types.json import Jsonb

from repopilot.agent.pg_recorder import PgRecorder
from repopilot.api import ApiServices, create_app
from repopilot.api.repo import RunsRepo
from repopilot.bench.tasks import load_tasks
from repopilot.eval.runner import PgResultStore

pytestmark = [pytest.mark.integration, pytest.mark.anyio]
TOKEN = "test-api-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeQueue:
    def __init__(self):
        self.jobs: list[dict] = []

    async def enqueue(self, job: dict) -> str:
        self.jobs.append(job)
        return f"{len(self.jobs)}-0"


@pytest.fixture
async def api(settings, conn):
    queue = FakeQueue()
    recorder = PgRecorder(settings.dsn())
    tasks = {t.id: t for t in load_tasks()}
    app = create_app(ApiServices(
        repo=RunsRepo(settings.dsn()), claims=recorder, queue=queue,
        results=PgResultStore(settings.dsn()), load_task=tasks.get, token=TOKEN,
        ui_origin="http://127.0.0.1:3000", default_model="test-model", poll_s=0.01))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://api") as client:
        yield client, queue, conn
    await recorder.close()


def _run(conn: psycopg.Connection, status: str = "running", **cols) -> str:
    run_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO runs (id, repo, issue_text, task_id, status, task_mode, approval_request,"
        " result) VALUES (%s, 'orders-api', 'issue', 'orders-api-005', %s, 'bench', %s, %s)",
        (run_id, status, Jsonb(cols.get("approval_request")), Jsonb(cols.get("result"))))
    return run_id


def _events(conn, run_id: str, n: int) -> None:
    for seq in range(1, n + 1):
        conn.execute("INSERT INTO run_events (run_id, seq, node, kind, payload)"
                     " VALUES (%s, %s, 'llm', 'reply', %s)", (run_id, seq, Jsonb({"i": seq})))


async def test_auth_required(api):
    client, _, _ = api
    assert (await client.get("/healthz")).status_code == 200
    for method, path in (("GET", "/v1/runs"), ("POST", "/v1/runs"), ("GET", "/v1/evals")):
        resp = await client.request(method, path, headers={"Authorization": "Bearer nope"})
        assert resp.status_code == 401 and resp.json()["error"]["code"] == "unauthorized"


async def test_create_bench_run_enqueues(api):
    client, queue, conn = api
    resp = await client.post("/v1/runs", json={"task_id": "orders-api-005"}, headers=AUTH)
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]
    assert queue.jobs == [{"type": "start", "run_id": run_id, "auto_approve": False}]
    row = conn.execute("SELECT status, task_mode, model FROM runs WHERE id = %s", (run_id,)).fetchone()
    assert row == ("queued", "bench", "test-model")


async def test_create_validates_body(api):
    client, queue, _ = api
    for body in ({}, {"task_id": "orders-api-005", "repo": "a/b", "issue_number": 1},
                 {"repo": "not a repo", "issue_number": 1}, {"repo": "a/b"},
                 {"task_id": "nope-001"}):
        resp = await client.post("/v1/runs", json=body, headers=AUTH)
        assert resp.status_code == 422, body
        assert resp.json()["error"]["code"] in ("invalid_request", "unknown_task")
    assert queue.jobs == []


async def test_create_github_run(api):
    client, _queue, conn = api
    resp = await client.post("/v1/runs", json={"repo": "acme/shop", "issue_number": 42},
                             headers=AUTH)
    run_id = resp.json()["run_id"]
    assert conn.execute("SELECT issue_ref, task_mode FROM runs WHERE id = %s",
                        (run_id,)).fetchone() == ("acme/shop#42", "github")


async def test_get_run_includes_approval_request(api):
    client, _, conn = api
    request = {"diff": "diff --git a/x b/x", "summary": "s", "tests": {"exit_code": 0}}
    run_id = _run(conn, "awaiting_approval", approval_request=request)
    body = (await client.get(f"/v1/runs/{run_id}", headers=AUTH)).json()
    assert body["status"] == "awaiting_approval" and body["approval_request"] == request
    assert (await client.get(f"/v1/runs/{uuid.uuid4()}", headers=AUTH)).status_code == 404


async def test_approval_exclusive(api):
    client, queue, conn = api
    run_id = _run(conn, "awaiting_approval")
    codes = []

    async def decide():
        resp = await client.post(f"/v1/runs/{run_id}/approval", json={"decision": "approve"},
                                 headers=AUTH)
        codes.append(resp.status_code)

    async with anyio.create_task_group() as tg:
        tg.start_soon(decide)
        tg.start_soon(decide)
    assert sorted(codes) == [202, 409]
    assert [j["type"] for j in queue.jobs] == ["resume"]


async def test_approval_wrong_state_409(api):
    client, _, conn = api
    run_id = _run(conn, "completed")
    resp = await client.post(f"/v1/runs/{run_id}/approval", json={"decision": "reject",
                                                                    "comment": "no"}, headers=AUTH)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "not_awaiting_approval"


async def test_events_stream_and_close(api):
    client, _, conn = api
    run_id = _run(conn, "completed")
    _events(conn, run_id, 3)
    resp = await client.get(f"/v1/runs/{run_id}/events", headers=AUTH)
    assert resp.headers["content-type"].startswith("text/event-stream")
    ids = [line[4:] for line in resp.text.splitlines() if line.startswith("id: ")]
    assert ids == ["1", "2", "3"] and "event: end" in resp.text


async def test_events_resume_from_last_id(api):
    client, _, conn = api
    run_id = _run(conn, "failed")
    _events(conn, run_id, 5)
    resp = await client.get(f"/v1/runs/{run_id}/events", headers=AUTH | {"Last-Event-ID": "3"})
    ids = [line[4:] for line in resp.text.splitlines() if line.startswith("id: ")]
    assert ids == ["4", "5"]


async def test_tool_calls_listed(api):
    client, _, conn = api
    run_id = _run(conn)
    conn.execute("INSERT INTO tool_calls (run_id, step, tool, args, ok, latency_ms)"
                 " VALUES (%s, 1, 'read_file', %s, true, 3)", (run_id, Jsonb({"path": "a.py"})))
    calls = (await client.get(f"/v1/runs/{run_id}/tool-calls", headers=AUTH)).json()["tool_calls"]
    assert [c["tool"] for c in calls] == ["read_file"] and calls[0]["args"] == {"path": "a.py"}


async def test_evals_endpoints(api):
    client, _, conn = api
    conn.execute(
        "INSERT INTO eval_results (eval_run, task_id, model, resolved, patch_applied,"
        " visible_tests_passed, retries, tool_calls, tool_failures, unauthorized_attempts,"
        " latency_ms, prompt_tokens, completion_tokens, cost_usd)"
        " VALUES ('e1', 'orders-api-001', 'm', true, true, true, 0, 3, 0, 0, 1000, 10, 2, 0)")
    listing = (await client.get("/v1/evals", headers=AUTH)).json()["evals"]
    assert listing[0]["name"] == "e1" and listing[0]["resolved"] == 1
    detail = (await client.get("/v1/evals/e1", headers=AUTH)).json()
    assert detail["summary"]["resolved"] == 1 and len(detail["results"]) == 1
    assert (await client.get("/v1/evals/nope", headers=AUTH)).status_code == 404


async def test_error_shape_and_headers(api):
    client, _, _ = api
    resp = await client.get("/v1/nothing-here", headers=AUTH | {"X-Trace-Id": "abc"})
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "not_found"
    assert resp.headers["X-Trace-Id"] == "abc"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
