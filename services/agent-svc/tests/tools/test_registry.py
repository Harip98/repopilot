import json

import pytest
from pydantic import BaseModel, Field

from repopilot.sandbox_client import SandboxError
from repopilot.tools.patch import patch_targets
from repopilot.tools.policy import ToolPolicy
from repopilot.tools.registry import (
    PolicyDenied,
    Tool,
    ToolContext,
    ToolError,
    ToolRegistry,
    truncate,
)

pytestmark = pytest.mark.anyio


def test_policy_denies():
    policy = ToolPolicy(protected_paths=["tests/heldout_*"])
    for path in (".git/config", ".github/workflows/ci.yml", "tests/heldout_pagination.py",
                 "../x", "/etc/x", "", "a\\b", "pkg/../../x"):
        assert policy.check_write(path) is not None, path
    for path in ("app/orders.py", "tests/test_orders.py", "./app//x.py", ".gitignore"):
        assert policy.check_write(path) is None, path


def test_policy_patch_size_and_targets():
    policy = ToolPolicy(max_patch_bytes=100)
    assert "limit is 100" in policy.check_patch("x" * 101)
    patch = "--- a/app/x.py\n+++ b/.github/workflows/ci.yml\n@@ -1 +1 @@\n-a\n+b\n"
    assert ".github" in ToolPolicy().check_patch(patch)
    ok = "--- a/app/x.py\n+++ b/app/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    assert ToolPolicy().check_patch(ok) is None


def test_patch_targets():
    diff = (
        "diff --git a/app/x.py b/app/x.py\n--- a/app/x.py\n+++ b/app/x.py\n@@ -1 +1 @@\n-a\n+b\n"
        "--- /dev/null\n+++ b/app/new.py\t2026-10-21 10:00:00\n@@ -0,0 +1 @@\n+c\n"
        "--- a/app/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-d\n"
        "diff --git a/app/a.py b/app/b.py\nsimilarity index 100%\nrename from app/a.py\nrename to app/b.py\n"
    )
    assert patch_targets(diff) == ["app/a.py", "app/b.py", "app/new.py", "app/old.py", "app/x.py"]


class EchoArgs(BaseModel):
    path: str
    count: int = Field(default=1, ge=1, le=5)


def _registry(calls: list, *, fail: Exception | None = None) -> ToolRegistry:
    async def echo(ctx: ToolContext, args: EchoArgs) -> str:
        calls.append(args)
        if fail:
            raise fail
        return f"{args.path} x{args.count}"

    return ToolRegistry([
        Tool("echo", "Echo a path.", EchoArgs, echo, "read"),
        Tool("write_echo", "Echo, as a write.", EchoArgs, echo, "write",
             check=lambda policy, args: policy.check_write(args.path)),
    ])


def _ctx() -> ToolContext:
    return ToolContext(sandbox=None, sandbox_id="0123456789abcdef", policy=ToolPolicy(),
                       test_command=["pytest", "-q"])


async def test_dispatch_ok():
    calls = []
    out = await _registry(calls).dispatch(_ctx(), "echo", json.dumps({"path": "a.py", "count": 2}))
    assert out.ok and out.content == "a.py x2" and not out.denied and out.error is None
    assert out.latency_ms >= 0


async def test_dispatch_bad_arguments():
    calls = []
    reg = _registry(calls)
    for raw, needle in (("{not json", "not valid JSON"), ("[1, 2]", "JSON object"),
                        ("{}", "path"), ('{"path": "a", "count": "many"}', "count"),
                        ('{"path": "a", "count": 9}', "count")):
        out = await reg.dispatch(_ctx(), "echo", raw)
        assert not out.ok and needle in out.error, (raw, out.error)
    assert calls == []


async def test_dispatch_unknown_tool():
    out = await _registry([]).dispatch(_ctx(), "rm_rf", "{}")
    assert not out.ok and "unknown tool" in out.error and "echo" in out.error


async def test_dispatch_policy_denied():
    calls = []
    out = await _registry(calls).dispatch(_ctx(), "write_echo", '{"path": ".git/hooks/pre-commit"}')
    assert not out.ok and out.denied and out.error.startswith("policy: ")
    assert calls == []


async def test_dispatch_sandbox_and_tool_errors():
    out = await _registry([], fail=SandboxError("not_found", "no such file", 404)).dispatch(
        _ctx(), "echo", '{"path": "x"}')
    assert not out.ok and not out.denied and out.error == "not_found: no such file"
    out = await _registry([], fail=ToolError("old text occurs 2 times")).dispatch(
        _ctx(), "echo", '{"path": "x"}')
    assert not out.ok and out.error == "old text occurs 2 times"


def test_truncate():
    assert truncate("abc", 10) == "abc"
    cut = truncate("x" * 9000, 8000)
    assert cut.startswith("x" * 8000) and cut.endswith("[truncated 1000 chars]")


def test_specs_and_subset():
    reg = _registry([])
    spec = next(s for s in reg.specs() if s.name == "echo")
    assert spec.parameters["required"] == ["path"]
    assert set(spec.parameters["properties"]) == {"path", "count"}
    assert "title" not in json.dumps(spec.parameters)
    assert [s.name for s in reg.subset({"read"}).specs()] == ["echo"]


async def test_dispatch_never_raises():
    out = await _registry([], fail=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")).dispatch(
        _ctx(), "echo", '{"path": "x"}')
    assert not out.ok and out.error.startswith("internal error: UnicodeDecodeError")
    out = await _registry([], fail=RuntimeError("x" * 20000)).dispatch(_ctx(), "echo", '{"path": "x"}')
    assert not out.ok and len(out.error) < 2100


async def test_dispatch_policy_denied_from_tool():
    out = await _registry([], fail=PolicyDenied(".github/ is protected")).dispatch(
        _ctx(), "echo", '{"path": "x"}')
    assert not out.ok and out.denied and out.error == "policy: .github/ is protected"


def test_policy_denies_nested_git_dirs():
    policy = ToolPolicy()
    for path in ("vendor/.git/config", "app/.GIT/HEAD", ".Git/x", "a/b/.git"):
        assert policy.check_write(path) is not None, path
    assert policy.check_write("app/.gitkeep") is None
