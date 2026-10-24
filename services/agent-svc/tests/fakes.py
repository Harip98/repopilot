"""Shared test doubles for the agent loop."""

import json

from repopilot.llm import AssistantMessage, ChatResult, ToolCall, Usage
from repopilot.sandbox_client import ExecResult, SandboxError

_ids = iter(range(1, 1_000_000))


def tool_reply(name: str, prompt_tokens: int = 100, **args) -> ChatResult:
    """A model turn that calls one tool."""
    call = ToolCall(id=f"call_{next(_ids)}", name=name, arguments=json.dumps(args))
    return ChatResult(
        message=AssistantMessage(content=None, tool_calls=[call]),
        usage=Usage(prompt_tokens=prompt_tokens, completion_tokens=20),
        finish_reason="tool_calls",
    )


def text_reply(content: str) -> ChatResult:
    return ChatResult(
        message=AssistantMessage(content=content),
        usage=Usage(prompt_tokens=100, completion_tokens=20),
        finish_reason="stop",
    )


def _result(exit_code: int = 0, stdout: str = "", stderr: str = "") -> ExecResult:
    return ExecResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=False,
        stderr_truncated=False,
        timed_out=False,
        duration_ms=5,
    )


class ScriptedSandbox:
    """In-memory workspace. Writes mark files as changed; git diff reflects them; the test
    command returns queued results (default: pass)."""

    def __init__(self, files: dict[str, bytes] | None = None, test_command=("pytest", "-q")):
        self.files = dict(files or {})
        self.changed: list[str] = []
        self.test_results: list[tuple[int, str]] = []
        self.test_command = list(test_command)
        self.execs: list[list[str]] = []
        self.destroyed: list[str] = []

    async def create(self, image: str) -> str:
        return "sbx-1"

    async def upload_dir(self, sandbox_id: str, src) -> None:
        pass

    async def read_file(self, sandbox_id: str, path: str) -> bytes:
        if path not in self.files:
            raise SandboxError("not_found", repr(path), 404)
        return self.files[path]

    async def write_file(self, sandbox_id: str, path: str, data: bytes) -> None:
        self.files[path] = data
        if not path.startswith(".git/") and path not in self.changed:
            self.changed.append(path)

    async def exec(self, sandbox_id, argv, timeout_s=120, cwd=None) -> ExecResult:
        self.execs.append(list(argv))
        if argv[:2] == ["git", "diff"] and "--name-status" in argv:
            return _result(stdout="".join(f"M\x00{p}\x00" for p in self.changed))
        if argv in (["git", "diff"], ["git", "diff", "HEAD"]):
            return _result(
                stdout="".join(f"diff --git a/{p} b/{p}\n+changed\n" for p in self.changed)
            )
        if list(argv[: len(self.test_command)]) == self.test_command:
            code, out = self.test_results.pop(0) if self.test_results else (0, "1 passed")
            return _result(code, out)
        if argv[0] == "find":
            return _result(stdout="".join(f"f {p}\n" for p in sorted(self.files)))
        return _result()

    async def destroy(self, sandbox_id: str) -> None:
        self.destroyed.append(sandbox_id)


class GitDirSandbox:
    """A real git repository in a host directory: git commands run for real, so diff and\n    status semantics match the sandbox. The test command returns a scripted result and can run
    a hook first, standing in for test code that touches the workspace."""

    def __init__(self, root, files: dict[str, bytes], test_command=("pytest", "-q")):
        import subprocess

        self.root, self.test_command = root, list(test_command)
        self.on_test = None
        self.test_results: list[tuple[int, str]] = []
        self.destroyed: list[str] = []
        for rel, data in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        for argv in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "base"]):
            subprocess.run(
                [
                    "git",
                    "-c",
                    "core.autocrlf=false",
                    "-c",
                    "user.name=t",
                    "-c",
                    "user.email=t@t",
                    *argv,
                ],
                cwd=root,
                check=True,
                capture_output=True,
            )

    async def read_file(self, sandbox_id: str, path: str) -> bytes:
        target = self.root / path
        if not target.is_file():
            raise SandboxError("not_found", repr(path), 404)
        return target.read_bytes()

    async def write_file(self, sandbox_id: str, path: str, data: bytes) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def exec(self, sandbox_id, argv, timeout_s=120, cwd=None) -> ExecResult:
        import subprocess

        if list(argv[: len(self.test_command)]) == self.test_command:
            if self.on_test:
                self.on_test(self.root)
            code, out = self.test_results.pop(0) if self.test_results else (0, "1 passed")
            return _result(code, out)
        if argv[0] != "git":
            return _result()
        proc = subprocess.run(  # noqa: ASYNC221 - test double; a blocking git call is fine here
            ["git", "-c", "core.autocrlf=false", "-c", "core.quotepath=false", *argv[1:]],
            cwd=self.root,
            capture_output=True,
            check=False,
        )
        return _result(proc.returncode, proc.stdout.decode(), proc.stderr.decode())

    async def destroy(self, sandbox_id: str) -> None:
        self.destroyed.append(sandbox_id)
