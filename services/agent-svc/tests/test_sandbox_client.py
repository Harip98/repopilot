import io
import json
import os
import tarfile
from pathlib import Path

import httpx
import pytest

from repopilot.sandbox_client import ExecResult, SandboxClient, SandboxError, workspace_tar

pytestmark = pytest.mark.anyio


def _client(handler) -> SandboxClient:
    return SandboxClient("http://sandbox", "tok-0123456789abcdef", transport=httpx.MockTransport(handler))


async def test_exec_parses_result():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "exit_code": 1, "stdout": "F", "stderr": "boom", "stdout_truncated": False,
            "stderr_truncated": True, "timed_out": False, "duration_ms": 420,
        })

    async with _client(handler) as sb:
        res = await sb.exec("0123456789abcdef", ["pytest", "-q"], timeout_s=30, cwd="pkg")
    assert res == ExecResult(exit_code=1, stdout="F", stderr="boom", stdout_truncated=False,
                             stderr_truncated=True, timed_out=False, duration_ms=420)
    assert seen["path"] == "/v1/sandboxes/0123456789abcdef/exec"
    assert seen["body"] == {"argv": ["pytest", "-q"], "timeout_s": 30, "cwd": "pkg"}


async def test_error_body_raises_sandbox_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": "bad_path", "message": "nope"}})

    async with _client(handler) as sb:
        with pytest.raises(SandboxError) as exc:
            await sb.read_file("0123456789abcdef", "../etc/passwd")
    assert (exc.value.code, exc.value.status, exc.value.message) == ("bad_path", 400, "nope")


async def test_non_json_error_still_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    async with _client(handler) as sb:
        with pytest.raises(SandboxError) as exc:
            await sb.destroy("0123456789abcdef")
    assert (exc.value.code, exc.value.status) == ("http_error", 502)


async def test_bearer_header_and_file_roundtrip():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.url.params.get("path"),
                      request.headers.get("authorization"), request.content))
        if request.method == "POST":
            return httpx.Response(201, json={"id": "0123456789abcdef"})
        if request.method == "GET":
            return httpx.Response(200, content=b"data")
        return httpx.Response(204)

    async with _client(handler) as sb:
        sid = await sb.create("repopilot-sandbox-base")
        await sb.write_file(sid, "a.py", b"x = 1\n")
        assert await sb.read_file(sid, "a.py") == b"data"
        await sb.destroy(sid)
    assert sid == "0123456789abcdef"
    assert all(auth == "Bearer tok-0123456789abcdef" for _, _, _, auth, _ in calls)
    assert calls[1][:3] == ("PUT", "/v1/sandboxes/0123456789abcdef/file", "a.py")
    assert calls[1][4] == b"x = 1\n"


def test_workspace_tar_modes_and_skips(tmp_path: Path):
    (tmp_path / "a.py").write_text("print(1)\n")
    (tmp_path / "run.sh").write_text("#!/bin/sh\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.pyc").write_bytes(b"\0")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n")

    with tarfile.open(fileobj=io.BytesIO(workspace_tar(tmp_path))) as tar:
        members = {m.name: m for m in tar.getmembers()}

    assert set(members) == {"a.py", "run.sh", ".git", ".git/HEAD", "pkg", "pkg/mod.py"}
    assert members["a.py"].mode == 0o644
    assert members["run.sh"].mode == 0o755
    assert members["pkg"].isdir() and members["pkg"].mode == 0o755
    assert all(m.uid == 10001 and m.gid == 10001 for m in members.values())


@pytest.mark.skipif(os.name == "nt", reason="creating symlinks needs privileges on Windows")
def test_workspace_tar_keeps_symlinks(tmp_path: Path):
    (tmp_path / "leak").symlink_to("/etc/passwd")
    with tarfile.open(fileobj=io.BytesIO(workspace_tar(tmp_path))) as tar:
        member = tar.getmember("leak")
    assert member.issym() and member.linkname == "/etc/passwd"
