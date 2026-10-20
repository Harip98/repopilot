import pytest

from repopilot.bench.tasks import load_tasks
from repopilot.bench.validate import validate_task
from repopilot.sandbox_client import get_sandbox_client

pytestmark = [pytest.mark.sandbox, pytest.mark.anyio]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize("task", load_tasks(), ids=lambda t: t.id)
async def test_validate_tasks(task):
    async with get_sandbox_client() as sandbox:
        result = await validate_task(task, sandbox)
    assert result.valid, result.model_dump()
