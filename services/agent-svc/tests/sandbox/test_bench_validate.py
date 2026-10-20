import pytest

from repopilot.bench.tasks import image_for, load_tasks
from repopilot.bench.validate import validate_task
from repopilot.bench.workspace import build_base_workspace
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


FORCE_PASS_HOOK = b"""
import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    report.outcome = "passed"
"""


async def test_conftest_hook_cannot_force_heldout_pass(tmp_path):
    """Even if an agent got a pass-forcing hook into tests/conftest.py, scoring restores the
    pristine conftest from heldout/ and the bug is still caught."""
    task = next(t for t in load_tasks() if t.id == "orders-api-004")
    ws = build_base_workspace(task, tmp_path / "ws")
    async with get_sandbox_client() as sandbox:
        sid = await sandbox.create(image_for(task))
        try:
            await sandbox.upload_dir(sid, ws)
            conftest = await sandbox.read_file(sid, "tests/conftest.py")
            await sandbox.write_file(sid, "tests/conftest.py", conftest + FORCE_PASS_HOOK)
            await sandbox.upload_dir(sid, task.heldout_dir)
            res = await sandbox.exec(sid, task.heldout_command, timeout_s=300)
            assert res.exit_code != 0, res.stdout
        finally:
            await sandbox.destroy(sid)
