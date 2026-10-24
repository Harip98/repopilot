from pathlib import Path

import pytest

from repopilot.mcp_server import WorkspaceTools, build_server
from tests.fakes import ScriptedSandbox

pytestmark = pytest.mark.anyio


def test_every_tool_takes_a_workspace():
    core = WorkspaceTools(ScriptedSandbox())
    schemas = {name: schema for name, _, schema in core.tool_schemas()}
    assert {"open_workspace", "close_workspace", "read_file", "apply_patch", "run_tests"} <= set(
        schemas
    )
    assert schemas["read_file"]["required"][0] == "workspace_id"
    assert "path" in schemas["read_file"]["properties"]
    assert build_server(core) is not None


async def test_open_use_close(tmp_path: Path):
    sandbox = ScriptedSandbox({"app.py": b"x = 1\n"})
    core = WorkspaceTools(sandbox, roots=[tmp_path])
    text, err = await core.call("open_workspace", {"path": str(tmp_path)})
    assert not err
    wid = __import__("json").loads(text)["workspace_id"]
    text, err = await core.call("read_file", {"workspace_id": wid, "path": "app.py"})
    assert not err and "x = 1" in text
    text, err = await core.call(
        "create_file", {"workspace_id": wid, "path": ".github/x.yml", "content": "on: push"}
    )
    assert err and "policy" in text  # same write policy as the agent
    assert (await core.call("close_workspace", {"workspace_id": wid})) == ("closed", False)
    assert sandbox.destroyed == [wid]


async def test_rejects_unknown_workspace_and_relative_paths():
    core = WorkspaceTools(ScriptedSandbox())
    assert (await core.call("read_file", {"workspace_id": "nope", "path": "a"}))[1]
    assert (await core.call("open_workspace", {"path": "relative/dir"}))[1]


async def test_open_is_limited_to_allowed_roots(tmp_path: Path):
    allowed, other = tmp_path / "work", tmp_path / "home"
    (allowed / "repo").mkdir(parents=True)
    (other / ".ssh").mkdir(parents=True)
    core = WorkspaceTools(ScriptedSandbox(), roots=[allowed])
    text, err = await core.call("open_workspace", {"path": str(other / ".ssh")})
    assert err and "outside" in text
    text, err = await core.call(
        "open_workspace", {"path": str(allowed / "repo" / ".." / ".." / "home")}
    )
    assert err
    assert not (await core.call("open_workspace", {"path": str(allowed / "repo")}))[1]


async def test_open_refuses_non_sandbox_images(tmp_path: Path):
    core = WorkspaceTools(ScriptedSandbox(), roots=[tmp_path])
    text, err = await core.call(
        "open_workspace", {"path": str(tmp_path), "image": "repopilot-agent-svc"}
    )
    assert err and "image" in text
