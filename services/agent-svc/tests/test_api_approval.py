"""POST /v1/runs/{id}/approval with in-memory doubles for the database and the queue."""

import httpx
import pytest

from repopilot.api import ApiServices, create_app

pytestmark = pytest.mark.anyio
TOKEN = "test-api-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
RUN = "6f1c1a2e-0000-4000-8000-000000000001"


class Rows:
    """The runs table (RunsRepo) and the claim (PgRecorder) in one."""

    def __init__(self):
        self.statuses = {RUN: "awaiting_approval"}

    async def get_run(self, run_id):
        return {"id": run_id, "status": self.statuses[run_id]} if run_id in self.statuses else None

    async def claim_for_approval(self, run_id):
        if self.statuses.get(run_id) != "awaiting_approval":
            return None
        self.statuses[run_id] = "running"
        return {"model": "m", "task_id": None, "sandbox_id": "s"}

    async def status(self, run_id, status, **fields):
        self.statuses[run_id] = status


class Queue:
    def __init__(self):
        self.jobs = []

    async def enqueue(self, job):
        self.jobs.append(job)
        return "1-0"


@pytest.fixture
async def api():
    rows, queue = Rows(), Queue()
    app = create_app(
        ApiServices(
            repo=rows,
            claims=rows,
            queue=queue,
            results=None,
            load_task=lambda task_id: None,
            token=TOKEN,
            ui_origin="http://127.0.0.1:3000",
            default_model="m",
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as client:
        yield client, rows, queue


@pytest.mark.parametrize(
    ("body", "by"),
    [
        ({"decision": "approve"}, "web"),
        ({"decision": "approve", "by": None}, "web"),
        ({"decision": "approve", "by": "  alice  "}, "alice"),
        ({"decision": "reject", "comment": "use casefold", "by": "a" * 64}, "a" * 64),
    ],
)
async def test_the_reviewer_name_goes_with_the_decision(api, body, by):
    client, _, queue = api
    resp = await client.post(f"/v1/runs/{RUN}/approval", json=body, headers=AUTH)
    assert resp.status_code == 202, resp.text
    [job] = queue.jobs
    assert job["decision"] == {
        "decision": body["decision"],
        "comment": body.get("comment"),
        "by": by,
    }


@pytest.mark.parametrize("by", ["", "   ", "a" * 65, "alice\nFixes #1", 7])
async def test_a_bad_reviewer_name_is_refused_before_the_claim(api, by):
    client, rows, queue = api
    resp = await client.post(
        f"/v1/runs/{RUN}/approval", json={"decision": "approve", "by": by}, headers=AUTH
    )
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "invalid_request"
    assert rows.statuses[RUN] == "awaiting_approval" and queue.jobs == []
