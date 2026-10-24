"""Where a run's timeline goes: status changes, graph events, every tool call, live usage
counters and reviewer decisions."""

from dataclasses import dataclass, field
from typing import Any, Protocol

from repopilot.llm.types import ToolCall
from repopilot.tools.registry import ToolOutcome


@dataclass
class ToolCallRecord:
    run_id: str
    step: int
    tool: str
    args: str
    ok: bool
    denied: bool
    error: str | None
    result_excerpt: str
    latency_ms: int
    refused: bool = False


class Recorder(Protocol):
    async def create_run(
        self,
        run_id: str,
        *,
        repo: str,
        issue_text: str,
        task_id: str | None,
        issue_ref: str | None,
        model: str,
        task_mode: str,
        sandbox_id: str | None = None,
    ) -> None: ...

    async def status(self, run_id: str, status: str, **fields: Any) -> None: ...

    async def event(self, run_id: str, node: str, kind: str, payload: dict) -> None: ...

    async def tool_call(
        self, run_id: str, step: int, call: ToolCall, outcome: ToolOutcome, *, refused: bool = False
    ) -> None:
        """Every non-control call the model requested. `refused`: the harness did not run it
        (not offered, unknown, or over budget); such a row takes the step of the last call
        that ran, so (step, id) keeps the run's order, and no usage counter counts it."""

    async def usage(self, run_id: str, usage: dict) -> None:
        """The run's counters so far (RunUsage fields), written while it runs."""

    async def approval(
        self, run_id: str, *, revision: int, decision: str, comment: str | None, by: str
    ) -> None:
        """A reviewer's decision on review round `revision`. The first write per round wins:
        the approval node re-runs from the top on resume."""


def tool_call_record(
    run_id: str, step: int, call: ToolCall, outcome: ToolOutcome, refused: bool = False
) -> ToolCallRecord:
    return ToolCallRecord(
        run_id=run_id,
        step=step,
        tool=call.name,
        args=call.arguments,
        ok=outcome.ok,
        denied=outcome.denied,
        error=outcome.error,
        result_excerpt=outcome.content[:2000],
        latency_ms=outcome.latency_ms,
        refused=refused,
    )


@dataclass
class MemoryRecorder:
    statuses: list[tuple[str, dict]] = field(default_factory=list)
    events: list[tuple[str, str, dict]] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)
    usages: list[dict] = field(default_factory=list)
    approvals: list[dict] = field(default_factory=list)

    async def create_run(self, run_id: str, **fields: Any) -> None:
        self.runs.append({"run_id": run_id} | fields)

    async def get_run(self, run_id: str) -> dict | None:
        return next((r for r in self.runs if r["run_id"] == run_id), None)

    async def status(self, run_id: str, status: str, **fields: Any) -> None:
        self.statuses.append((status, fields))

    async def event(self, run_id: str, node: str, kind: str, payload: dict) -> None:
        self.events.append((node, kind, payload))

    async def tool_call(
        self, run_id: str, step: int, call: ToolCall, outcome: ToolOutcome, *, refused: bool = False
    ) -> None:
        self.tool_calls.append(tool_call_record(run_id, step, call, outcome, refused))

    async def usage(self, run_id: str, usage: dict) -> None:
        self.usages.append(dict(usage))

    async def approval(
        self, run_id: str, *, revision: int, decision: str, comment: str | None, by: str
    ) -> None:
        if any(a["run_id"] == run_id and a["revision"] == revision for a in self.approvals):
            return  # as in Postgres: the first decision of a round wins
        self.approvals.append(
            {
                "run_id": run_id,
                "revision": revision,
                "decision": decision,
                "comment": comment,
                "by": by,
            }
        )
