import uuid

import pytest

from repopilot.config import get_settings
from repopilot.queue import JobQueue
from repopilot.worker import Worker

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
async def queue():
    url = get_settings().redis_url.rsplit("/", 1)[0] + "/15"  # never the dev database
    q = JobQueue(url, stream=f"test:jobs:{uuid.uuid4().hex[:8]}")
    try:
        await q.ensure_group()
    except OSError as exc:
        pytest.fail(f"Redis not reachable - run `docker compose up -d redis`: {exc}")
    yield q
    await q.drop()
    await q.close()


async def test_enqueue_next_ack(queue: JobQueue):
    entry = await queue.enqueue({"type": "start", "run_id": "r1"})
    delivery = await queue.next("w1", block_ms=500)
    assert delivery.id == entry and delivery.job == {"type": "start", "run_id": "r1"}
    assert delivery.deliveries == 1
    await queue.ack(delivery.id)
    assert await queue.next("w1", block_ms=100) is None
    assert await queue.reclaim("w2", min_idle_ms=0) == []


async def test_reclaim_after_idle(queue: JobQueue):
    await queue.enqueue({"type": "start", "run_id": "r2"})
    first = await queue.next("w1", block_ms=500)  # w1 "dies" without acking
    assert await queue.reclaim("w2", min_idle_ms=60_000) == []  # not idle long enough
    [again] = await queue.reclaim("w2", min_idle_ms=0)
    assert again.id == first.id and again.job["run_id"] == "r2" and again.deliveries == 2


async def test_retries_exhausted_marks_failed(queue: JobQueue):
    await queue.enqueue({"type": "start", "run_id": "r3"})
    exhausted = []

    async def handle(job):
        raise RuntimeError("worker bug")

    async def on_exhausted(job):
        exhausted.append(job["run_id"])

    worker = Worker(queue, handle, "w1", visibility_ms=0, max_deliveries=3,
                    on_exhausted=on_exhausted)
    for _ in range(5):
        await worker.run_once(block_ms=50)
    assert exhausted == ["r3"]
    assert await queue.reclaim("w1", min_idle_ms=0) == []  # acked after giving up


async def test_lease_is_extended_while_a_job_runs(queue: JobQueue):
    import anyio

    await queue.enqueue({"type": "start", "run_id": "r4"})
    stolen = []

    async def slow(job):
        for _ in range(8):
            await anyio.sleep(0.1)
            stolen.extend(await queue.reclaim("w2", min_idle_ms=300))

    await Worker(queue, slow, "w1", visibility_ms=300).run_once(block_ms=200)
    assert stolen == []  # w1 kept its lease for the whole 0.8 s job
