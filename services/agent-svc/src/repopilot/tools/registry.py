"""Tool specs for the model and safe dispatch of its tool calls.

Dispatch never raises: bad JSON, bad arguments, policy denials, sandbox errors and tool
failures all come back as a ToolOutcome the model can read and react to.
"""

import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from repopilot.llm.types import ToolSpec
from repopilot.sandbox_client import SandboxClient, SandboxError
from repopilot.tools.policy import ToolPolicy

ToolKind = Literal["read", "write", "exec"]
MAX_CONTENT = 8000
MAX_ERROR = 2000
log = logging.getLogger(__name__)


class ToolError(Exception):
    """A failure the model should see verbatim (e.g. "old text occurs 2 times")."""


class PolicyDenied(ToolError):
    """Raised by a tool that discovers a forbidden target only while running (e.g. the paths
    git says a patch would touch). Recorded as a denial, like a failed pre-check."""


class ToolOutcome(BaseModel):
    ok: bool
    content: str = ""
    denied: bool = False
    error: str | None = None
    latency_ms: int = 0

    def for_model(self) -> str:
        """The text sent back to the model as the tool result."""
        return self.content if self.ok else f"ERROR: {self.error}"


@dataclass
class ToolContext:
    sandbox: SandboxClient
    sandbox_id: str
    policy: ToolPolicy
    test_command: list[str]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    fn: Callable[[ToolContext, Any], Awaitable[str]]
    kind: ToolKind
    check: Callable[[ToolPolicy, Any], str | None] | None = None  # policy gate for writes


def truncate(text: str, limit: int = MAX_CONTENT) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n…[truncated {len(text) - limit} chars]"


def drop_titles(schema: Any) -> Any:
    """Pydantic adds a title to every node; they cost tokens and tell the model nothing."""
    if isinstance(schema, dict):
        return {k: drop_titles(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [drop_titles(v) for v in schema]
    return schema


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "arguments"
        parts.append(f"{loc}: {err['msg']}")
    return "invalid arguments: " + "; ".join(parts)


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool]):
        self._tools = {t.name: t for t in tools}

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(name=t.name, description=t.description,
                     parameters=drop_titles(t.args_model.model_json_schema()))
            for t in self._tools.values()
        ]

    def subset(self, kinds: set[ToolKind]) -> "ToolRegistry":
        return ToolRegistry(t for t in self._tools.values() if t.kind in kinds)

    def kind_of(self, name: str) -> ToolKind | None:
        tool = self._tools.get(name)
        return tool.kind if tool else None

    async def dispatch(self, ctx: ToolContext, name: str, arguments: str) -> ToolOutcome:
        start = time.perf_counter()

        def done(**kw: Any) -> ToolOutcome:
            return ToolOutcome(latency_ms=int((time.perf_counter() - start) * 1000), **kw)

        tool = self._tools.get(name)
        if tool is None:
            return done(ok=False, error=f"unknown tool {name!r}; available: {', '.join(self._tools)}")
        try:
            raw = json.loads(arguments or "{}")
        except json.JSONDecodeError as exc:
            return done(ok=False, error=f"arguments are not valid JSON: {exc}")
        if not isinstance(raw, dict):
            return done(ok=False, error="arguments must be a JSON object")
        try:
            args = tool.args_model.model_validate(raw)
        except ValidationError as exc:
            return done(ok=False, error=_validation_message(exc))
        if tool.check and (reason := tool.check(ctx.policy, args)):
            return done(ok=False, denied=True, error=f"policy: {reason}")
        try:
            content = await tool.fn(ctx, args)
        except PolicyDenied as exc:
            return done(ok=False, denied=True, error=f"policy: {exc}")
        except SandboxError as exc:
            return done(ok=False, error=truncate(f"{exc.code}: {exc.message}", MAX_ERROR))
        except ToolError as exc:
            return done(ok=False, error=truncate(str(exc), MAX_ERROR))
        except Exception as exc:
            log.exception("tool %s failed", name)
            return done(ok=False, error=truncate(
                f"internal error: {type(exc).__name__}: {exc}", MAX_ERROR))
        return done(ok=True, content=truncate(content))
