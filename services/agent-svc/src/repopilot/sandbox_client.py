"""Async client for sandbox-svc, the only component allowed to start containers."""

import io
import os
import tarfile
from pathlib import Path
from types import TracebackType
from typing import Self

import httpx
from pydantic import BaseModel

from repopilot.config import get_settings

SANDBOX_UID = 10001
SKIP_DIRS = frozenset({"__pycache__", ".venv"})


class ExecResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    duration_ms: int


class SandboxError(Exception):
    """A non-2xx answer from sandbox-svc, carrying its error code."""

    def __init__(self, code: str, message: str, status: int):
        super().__init__(f"{status} {code}: {message}")
        self.code, self.message, self.status = code, message, status


def _owned(info: tarfile.TarInfo, mode: int) -> tarfile.TarInfo:
    info.mode, info.uid, info.gid, info.uname, info.gname = mode, SANDBOX_UID, SANDBOX_UID, "", ""
    return info


def workspace_tar(src: Path) -> bytes:
    """Pack `src` (including .git) for upload into a sandbox's /workspace.

    Modes are normalised so a Windows checkout behaves like a POSIX one, and everything is
    owned by the sandbox user. Symlinks are kept as links; the sandbox refuses to read them.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for root, dirs, files in os.walk(src):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            root_path = Path(root)
            for name in dirs + sorted(files):
                path = root_path / name
                arcname = path.relative_to(src).as_posix()
                if path.is_symlink():
                    info = tarfile.TarInfo(arcname)
                    info.type, info.linkname = tarfile.SYMTYPE, os.readlink(path)
                    tar.addfile(_owned(info, 0o777))
                elif path.is_dir():
                    tar.addfile(_owned(tar.gettarinfo(path, arcname), 0o755))
                else:
                    executable = name.endswith(".sh") or (
                        os.name != "nt" and path.stat().st_mode & 0o111
                    )
                    info = _owned(tar.gettarinfo(path, arcname), 0o755 if executable else 0o644)
                    with path.open("rb") as fh:
                        tar.addfile(info, fh)
    return buf.getvalue()


class SandboxClient:
    def __init__(
        self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None
    ):
        self._http = httpx.AsyncClient(
            base_url=base_url, transport=transport, timeout=httpx.Timeout(30.0),
            headers={"Authorization": f"Bearer {token}"},
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        resp = await self._http.request(method, url, **kwargs)
        if resp.is_success:
            return resp
        try:
            err = resp.json()["error"]
            raise SandboxError(err["code"], err["message"], resp.status_code)
        except (ValueError, KeyError, TypeError):
            raise SandboxError("http_error", resp.text[:500], resp.status_code) from None

    async def create(self, image: str) -> str:
        resp = await self._request("POST", "/v1/sandboxes", json={"image": image})
        return resp.json()["id"]

    async def upload_dir(self, sandbox_id: str, src: Path) -> None:
        await self.upload_tar(sandbox_id, workspace_tar(src))

    async def upload_tar(self, sandbox_id: str, data: bytes) -> None:
        """Extract a tar archive into /workspace. sandbox-svc sanitises it and rejects entries
        that could land outside the workspace (code `bad_archive`)."""
        await self._request(
            "PUT", f"/v1/sandboxes/{sandbox_id}/archive", content=data,
            headers={"Content-Type": "application/x-tar"}, timeout=300.0,
        )

    async def read_file(self, sandbox_id: str, path: str) -> bytes:
        resp = await self._request("GET", f"/v1/sandboxes/{sandbox_id}/file", params={"path": path})
        return resp.content

    async def write_file(self, sandbox_id: str, path: str, data: bytes) -> None:
        await self._request(
            "PUT", f"/v1/sandboxes/{sandbox_id}/file", params={"path": path}, content=data,
            headers={"Content-Type": "application/octet-stream"},
        )

    async def exec(
        self, sandbox_id: str, argv: list[str], timeout_s: int = 120, cwd: str | None = None
    ) -> ExecResult:
        body: dict = {"argv": argv, "timeout_s": timeout_s}
        if cwd is not None:
            body["cwd"] = cwd
        # The server may hold the request for timeout + kill grace + backstop; wait longer.
        resp = await self._request(
            "POST", f"/v1/sandboxes/{sandbox_id}/exec", json=body, timeout=timeout_s + 60.0
        )
        return ExecResult.model_validate(resp.json())

    async def destroy(self, sandbox_id: str) -> None:
        await self._request("DELETE", f"/v1/sandboxes/{sandbox_id}")


def get_sandbox_client() -> SandboxClient:
    settings = get_settings()
    return SandboxClient(settings.sandbox_url, settings.sandbox_token.get_secret_value())
