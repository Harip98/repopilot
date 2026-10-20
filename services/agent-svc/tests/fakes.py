"""Shared test doubles for the agent loop."""

import json

from repopilot.llm import AssistantMessage, ChatResult, ToolCall, Usage
from repopilot.sandbox_client import ExecResult, SandboxError

_ids = iter(range(1, 1_000_000))


def tool_reply(name: str, prompt_tokens: int = 100, **args) -> ChatResult:
    """A model turn that calls one tool."""
    call = ToolCall(id=f"call_{next(_ids)}", name=name, arguments=json.dumps(args))
    return ChatResult(message=AssistantMessage(content=None, tool_calls=[call]),
                      usage=Usage(prompt_tokens=prompt_tokens, completion_tokens=20),
                      finish_reason="tool_calls")


def text_reply(content: str) -> ChatResult:
    return ChatResult(message=AssistantMessage(content=content),
                      usage=Usage(prompt_tokens=100, completion_tokens=20), finish_reason="stop")


def _result(exit_code: int = 0, stdout: str = "", stderr: str = "") -> ExecResult:
    return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr, stdout_truncated=False,
                      stderr_truncated=False, timed_out=False, duration_ms=5)


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
        if argv == ["git", "diff"]:
            return _result(stdout="".join(f"diff --git a/{p} b/{p}\n+changed\n" for p in self.changed))
        if list(argv[:len(self.test_command)]) == self.test_command:
            code, out = self.test_results.pop(0) if self.test_results else (0, "1 passed")
            return _result(code, out)
        return _result()

    async def destroy(self, sandbox_id: str) -> None:
        self.destroyed.append(sandbox_id)
