"""Everything the agent graph checkpoints. Dependencies (LLM, sandbox, recorder) live in
RunDeps instead, so a checkpoint is plain JSON and a run can resume in another process."""

from typing import Literal

from pydantic import BaseModel, Field

Phase = Literal["explore", "edit"]
Route = Literal[
    "llm",
    "tools",
    "nudge",
    "validate",
    "diagnose",
    "request_review",
    "approval",
    "open_pr",
    "fail",
    "record",
    "end",
]
RunStatus = Literal[
    "running", "awaiting_approval", "approved", "rejected", "pr_opened", "completed", "failed"
]


class Plan(BaseModel):
    root_cause: str
    files: list[str]
    acceptance_criteria: list[str]
    test_targets: list[str] = Field(default_factory=list)


class Budget(BaseModel):
    max_llm_calls: int = 40
    max_tool_calls: int = 80
    max_tokens: int = 600_000
    max_seconds: float = 1200.0
    max_retries: int = 3
    max_explore_steps: int = 15


class RunUsage(BaseModel):
    llm_calls: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    unauthorized_attempts: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


class TestReport(BaseModel):
    __test__ = False  # not a pytest test class

    passed: bool
    reason: str | None = None  # no_changes | protected_paths_changed | tests_failing
    exit_code: int | None = None
    output_tail: str = ""
    changed_files: list[str] = Field(default_factory=list)
    violations: list[str] = Field(default_factory=list)


class Approval(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str | None = None
    by: str


class GitHubTarget(BaseModel):
    """Where an approved change goes: the issue it fixes and the commit it was made against."""

    repo: str  # owner/name
    issue_number: int
    issue_url: str
    base_branch: str
    base_sha: str


class RunState(BaseModel):
    run_id: str
    github: GitHubTarget | None = None
    task_id: str | None = None
    repo: str
    issue_title: str
    issue_text: str
    sandbox_id: str
    test_command: list[str]
    protected_paths: list[str] = Field(default_factory=list)
    auto_approve: bool = False
    baseline_tests_passed: bool | None = None  # test command result before any change

    phase: Phase = "explore"
    route: Route = "llm"
    messages: list[dict] = Field(default_factory=list)
    offered_tools: list[str] = Field(default_factory=list)  # what the last reply could call
    plan: Plan | None = None
    summary: str | None = None
    finish_called: bool = False

    retries: int = 0
    nudges: int = 0
    revisions: int = 0
    explore_steps: int = 0

    diff: str = ""
    last_tests: TestReport | None = None
    usage: RunUsage = Field(default_factory=RunUsage)
    budget: Budget = Field(default_factory=Budget)
    started_at: float = 0.0
    validated_at: float = 0.0  # when the last validation ran; pauses for review start here

    approval: Approval | None = None
    status: RunStatus = "running"
    failure_reason: str | None = None
    pr_url: str | None = None


def budget_exceeded(state: RunState, now: float, *, llm_calls: bool = True) -> str | None:
    """The first budget that leaves no room for another step, or None. The tools node passes
    llm_calls=False: usage already counts the model call whose reply it runs, a call that was
    within budget when it was made."""
    u, b = state.usage, state.budget
    if llm_calls and u.llm_calls >= b.max_llm_calls:
        return "llm_calls"
    if u.tool_calls >= b.max_tool_calls:
        return "tool_calls"
    if u.prompt_tokens + u.completion_tokens >= b.max_tokens:
        return "tokens"
    if now - state.started_at >= b.max_seconds:
        return "seconds"
    return None
