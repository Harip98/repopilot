"""Live isolation tests against the compose sandbox-svc. Each test gets a fresh sandbox."""

import io
import os
import subprocess
import tarfile
import time
from pathlib import Path

import anyio
import httpx
import pytest

from repopilot.config import get_settings
from repopilot.sandbox_client import SandboxClient, SandboxError, get_sandbox_client

pytestmark = [pytest.mark.sandbox, pytest.mark.anyio]

IMAGE = "repopilot-sandbox-base"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "mod.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\npythonpath = ["."]\n')
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mod.py").write_text(
        "from pkg.mod import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    try:
        (tmp_path / "leak").symlink_to("/etc/passwd")
    except OSError:  # Windows without symlink privilege: the test below creates it in-sandbox
        pass
    return tmp_path


@pytest.fixture
async def sandbox(repo: Path):
    try:
        client = get_sandbox_client()
        sid = await client.create(IMAGE)
    except httpx.ConnectError as exc:
        pytest.fail(f"sandbox-svc not reachable - run `docker compose up -d sandbox-svc`: {exc}")
    await client.upload_dir(sid, repo)
    try:
        yield client, sid
    finally:
        try:
            await client.destroy(sid)
        except SandboxError:
            pass
        await client.aclose()


async def test_upload_read_write_roundtrip(sandbox):
    client, sid = sandbox
    assert b"return a + b" in await client.read_file(sid, "pkg/mod.py")
    await client.write_file(sid, "pkg/new/deep.py", b"VALUE = 42\n")
    assert await client.read_file(sid, "pkg/new/deep.py") == b"VALUE = 42\n"
    res = await client.exec(sid, ["python", "-c", "import pkg.new.deep as d; print(d.VALUE)"])
    assert res.exit_code == 0 and res.stdout.strip() == "42"


async def test_exec_python_and_pytest(sandbox):
    client, sid = sandbox
    res = await client.exec(sid, ["pytest", "-q", "tests/test_mod.py"], timeout_s=60)
    assert res.exit_code == 0, res.stdout + res.stderr
    assert "1 passed" in res.stdout


async def test_git_works_on_uploaded_repo(sandbox):
    client, sid = sandbox
    assert (await client.exec(sid, ["git", "init", "-q"])).exit_code == 0
    assert (await client.exec(sid, ["git", "add", "-A"])).exit_code == 0
    res = await client.exec(sid, ["git", "commit", "-qm", "base"])
    assert res.exit_code == 0, res.stderr
    await client.write_file(sid, "pkg/mod.py", b"def add(a, b):\n    return a - b\n")
    diff = await client.exec(sid, ["git", "diff"])
    assert "-    return a + b" in diff.stdout and "+    return a - b" in diff.stdout


async def test_no_network(sandbox):
    client, sid = sandbox
    res = await client.exec(
        sid, ["python", "-c", "import socket; socket.create_connection(('1.1.1.1', 53), 2)"]
    )
    assert res.exit_code != 0
    assert "unreachable" in res.stderr.lower()


async def test_rootfs_read_only(sandbox):
    client, sid = sandbox
    res = await client.exec(sid, ["python", "-c", "open('/etc/x', 'w')"])
    assert res.exit_code != 0 and "Read-only" in res.stderr


async def test_runs_unprivileged(sandbox):
    client, sid = sandbox
    res = await client.exec(sid, ["python", "-c", "import os; print(os.getuid(), os.getgid())"])
    assert res.stdout.strip() == "10001 10001"


async def test_timeout_fires(sandbox):
    client, sid = sandbox
    start = time.monotonic()
    res = await client.exec(sid, ["python", "-c", "import time; time.sleep(30)"], timeout_s=2)
    assert res.timed_out
    assert time.monotonic() - start < 15


async def test_background_child_cannot_hang_exec(sandbox):
    """A child that escapes the process group and holds stdout open is cut off by the backstop,
    and the workspace survives."""
    client, sid = sandbox
    script = (
        "import subprocess; subprocess.Popen(['sleep', '300'], start_new_session=True); "
        "print('spawned', flush=True)"
    )
    start = time.monotonic()
    res = await client.exec(sid, ["python", "-c", script], timeout_s=2)
    assert time.monotonic() - start < 40
    # Docker 29 ends the stream when the exec'd process exits, so this normally returns at
    # once; on daemons that wait for every stdout holder, the backstop fires instead.
    if res.exit_code == -1:
        assert res.timed_out
    after = await client.exec(sid, ["python", "-c", "print('alive')"])
    assert after.stdout.strip() == "alive"
    assert b"return a + b" in await client.read_file(sid, "pkg/mod.py")


async def test_fork_bomb_contained(sandbox):
    client, sid = sandbox
    bomb = "import os\nwhile True:\n    try:\n        os.fork()\n    except OSError:\n        pass\n"
    start = time.monotonic()
    await client.exec(sid, ["python", "-c", bomb], timeout_s=10)
    assert time.monotonic() - start < 60
    res = await client.exec(sid, ["python", "-c", "print(1)"])
    assert res.exit_code == 0 and res.stdout.strip() == "1"


async def test_output_truncated(sandbox):
    client, sid = sandbox
    res = await client.exec(sid, ["python", "-c", "print('x' * 1_000_000)"])
    assert res.exit_code == 0
    assert res.stdout_truncated and len(res.stdout) <= 65536


async def test_symlink_read_rejected(sandbox, repo: Path):
    client, sid = sandbox
    if not (repo / "leak").is_symlink():
        res = await client.exec(
            sid, ["python", "-c", "import os; os.symlink('/etc/passwd', 'leak')"]
        )
        assert res.exit_code == 0, res.stderr
    with pytest.raises(SandboxError) as exc:
        await client.read_file(sid, "leak")
    assert exc.value.code == "not_a_file"


async def test_path_traversal_rejected(sandbox):
    client, sid = sandbox
    for path in ("../etc/passwd", "/etc/passwd", "pkg/../../etc/passwd"):
        with pytest.raises(SandboxError) as exc:
            await client.read_file(sid, path)
        assert exc.value.code == "bad_path", path


async def test_missing_file_not_found(sandbox):
    client, sid = sandbox
    with pytest.raises(SandboxError) as exc:
        await client.read_file(sid, "nope.py")
    assert (exc.value.code, exc.value.status) == ("not_found", 404)


async def test_image_not_allowed():
    async with get_sandbox_client() as client:
        with pytest.raises(SandboxError) as exc:
            await client.create("python:3.12-slim")
    assert exc.value.code == "image_not_allowed"


async def test_shell_not_allowed(sandbox):
    client, sid = sandbox
    with pytest.raises(SandboxError) as exc:
        await client.exec(sid, ["sh", "-c", "id"])
    assert exc.value.code == "command_not_allowed"


async def test_unauthenticated_rejected():
    async with SandboxClient(get_settings().sandbox_url, "wrong-token-0123456789") as client:
        with pytest.raises(SandboxError) as exc:
            await client.create(IMAGE)
    assert exc.value.status == 401


async def test_destroy_removes_volume(repo: Path):
    async with get_sandbox_client() as client:
        sid = await client.create(IMAGE)
        await client.destroy(sid)
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
    # In a thread: the selector event loop used on Windows (for psycopg) has no subprocesses.
    done = await anyio.to_thread.run_sync(lambda: subprocess.run(
        ["docker", "volume", "ls", "-q", "-f", f"name=rp-ws-{sid}"], env=env,
        capture_output=True, text=True, check=True))
    assert done.stdout.strip() == ""


def _tar(*members: tuple[tarfile.TarInfo, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for info, data in members:
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data) if data else None)
    return buf.getvalue()


def _link(name: str, target: str) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.type, info.linkname = tarfile.SYMTYPE, target
    return info, b""


def _file(name: str, data: bytes, mode: int = 0o644) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.mode = mode
    return info, data


async def _exists(client, sid, path: str) -> bool:
    res = await client.exec(sid, ["python", "-c", f"import os; print(os.path.lexists({path!r}))"])
    return res.stdout.strip() == "True"


async def test_upload_through_symlink_rejected(sandbox):
    client, sid = sandbox
    for archive in (
        _tar(_link("evil", "/etc"), _file("evil/pwned", b"x")),
        _tar(_link("b", "/usr/bin"), _file("b/timeout", b"#!/bin/sh\n", 0o755)),
    ):
        with pytest.raises(SandboxError) as exc:
            await client.upload_tar(sid, archive)
        assert exc.value.code == "bad_archive"
    assert not await _exists(client, sid, "/etc/pwned")
    res = await client.exec(sid, ["python", "-c", "import time; time.sleep(30)"], timeout_s=2)
    assert res.timed_out  # coreutils timeout was not replaced


async def test_upload_replaces_planted_symlink(sandbox):
    """A symlink planted in the workspace before a later upload (e.g. held-out tests) is
    replaced by a real directory, never followed."""
    client, sid = sandbox
    res = await client.exec(sid, ["python", "-c", "import os; os.symlink('/tmp', 'heldout')"])
    assert res.exit_code == 0, res.stderr
    await client.upload_tar(sid, _tar(_file("heldout/test_x.py", b"def test_x(): pass\n")))
    assert not await _exists(client, sid, "/tmp/test_x.py")
    assert await client.read_file(sid, "heldout/test_x.py") == b"def test_x(): pass\n"


async def test_upload_strips_setuid_and_devices(sandbox):
    client, sid = sandbox
    dev = tarfile.TarInfo("mem")
    dev.type, dev.devmajor, dev.devminor = tarfile.CHRTYPE, 1, 1
    with pytest.raises(SandboxError) as exc:
        await client.upload_tar(sid, _tar((dev, b"")))
    assert exc.value.code == "bad_archive"
    await client.upload_tar(sid, _tar(_file("suid", b"x", 0o4755)))
    res = await client.exec(sid, ["python", "-c", "import os; print(oct(os.stat('suid').st_mode & 0o7777))"])
    assert res.stdout.strip() == "0o755"


async def test_workspace_mounted_nodev_nosuid(sandbox):
    client, sid = sandbox
    res = await client.exec(sid, ["cat", "/proc/mounts"])
    line = next(ln for ln in res.stdout.splitlines() if " /workspace " in ln)
    assert "nodev" in line and "nosuid" in line, line


async def test_symlinked_dir_read_rejected(sandbox):
    client, sid = sandbox
    res = await client.exec(sid, ["python", "-c", "import os; os.symlink('/etc', 'e2')"])
    assert res.exit_code == 0, res.stderr
    for path in ("e2/passwd", "e2/shadow", "e2/hostname"):
        with pytest.raises(SandboxError) as exc:
            await client.read_file(sid, path)
        assert exc.value.code == "not_a_file", path


async def test_read_binary_file_roundtrip(sandbox):
    client, sid = sandbox
    data = bytes(range(256)) * 100
    await client.write_file(sid, "blob.bin", data)
    assert await client.read_file(sid, "blob.bin") == data


async def test_escaped_fork_bomb_recovers(sandbox):
    """A fork bomb that leaves timeout's process group must not brick the sandbox."""
    client, sid = sandbox
    bomb = (
        "import os\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    while True:\n"
        "        try:\n"
        "            os.fork()\n"
        "        except OSError:\n"
        "            pass\n"
    )
    try:
        await client.exec(sid, ["python", "-c", bomb], timeout_s=5)
    except SandboxError as exc:
        assert exc.code == "sandbox_exhausted"
    for _ in range(2):  # the first call after a reset may still report exhaustion
        try:
            res = await client.exec(sid, ["python", "-c", "print('alive')"])
            break
        except SandboxError as exc:
            assert exc.code == "sandbox_exhausted"
    assert res.stdout.strip() == "alive"
    assert b"return a + b" in await client.read_file(sid, "pkg/mod.py")
