"""Deterministic scoring of eval runs: rates with Wilson intervals, latency percentiles, cost.

No LLM judge anywhere: a task is resolved when the run passed harness validation and the
held-out tests pass on its final workspace.
"""

import math
from collections import Counter

from pydantic import BaseModel, Field


class EvalResult(BaseModel):
    eval_run: str
    task_id: str
    run_id: str | None
    model: str
    difficulty: str
    split: str
    status: str  # final run status, or "error" when the harness itself failed
    failure_reason: str | None
    resolved: bool
    patch_applied: bool
    visible_tests_passed: bool
    baseline_tests_passed: bool | None
    retries: int
    llm_calls: int
    tool_calls: int
    tool_failures: int
    unauthorized_attempts: int
    test_files_modified: bool
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    heldout_exit_code: int | None
    error: str | None


class Summary(BaseModel):
    n: int
    resolved: int
    errors: int
    resolution_rate: float | None
    resolution_ci: tuple[float | None, float | None]
    patch_applied_rate: float | None
    visible_pass_rate: float | None
    baseline_pass_rate: float | None
    tool_calls: int
    tool_failure_rate: float | None
    avg_retries: float | None
    p50_latency_s: float | None
    p95_latency_s: float | None
    prompt_tokens: int
    completion_tokens: int
    tokens_per_resolved: float | None
    cost_usd: float
    cost_per_resolved: float | None
    unauthorized_attempts: int
    runs_with_test_edits: int
    failure_reasons: dict[str, int] = Field(default_factory=dict)
    by_difficulty: dict[str, "Summary"] = Field(default_factory=dict)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    """95% Wilson score interval for k successes out of n."""
    if n == 0:
        return None, None
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile: always one of the observed values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _rate(k: int, n: int) -> float | None:
    return None if n == 0 else k / n


def summarize(results: list[EvalResult], *, split_difficulty: bool = True) -> Summary:
    n = len(results)
    resolved = sum(r.resolved for r in results)
    tool_calls = sum(r.tool_calls for r in results)
    tokens = sum(r.prompt_tokens + r.completion_tokens for r in results)
    cost = sum(r.cost_usd for r in results)
    baseline = [r.baseline_tests_passed for r in results if r.baseline_tests_passed is not None]
    latencies = [r.latency_ms / 1000 for r in results]
    reasons = Counter(
        "error" if r.status == "error" else (r.failure_reason or r.status)
        for r in results if not r.resolved
    )
    by_difficulty = {}
    if split_difficulty:
        for level in sorted({r.difficulty for r in results}):
            by_difficulty[level] = summarize([r for r in results if r.difficulty == level],
                                             split_difficulty=False)
    return Summary(
        n=n, resolved=resolved, errors=sum(r.status == "error" for r in results),
        resolution_rate=_rate(resolved, n), resolution_ci=wilson(resolved, n),
        patch_applied_rate=_rate(sum(r.patch_applied for r in results), n),
        visible_pass_rate=_rate(sum(r.visible_tests_passed for r in results), n),
        baseline_pass_rate=_rate(sum(baseline), len(baseline)),
        tool_calls=tool_calls,
        tool_failure_rate=_rate(sum(r.tool_failures for r in results), tool_calls),
        avg_retries=_rate(sum(r.retries for r in results), n),
        p50_latency_s=percentile(latencies, 50), p95_latency_s=percentile(latencies, 95),
        prompt_tokens=sum(r.prompt_tokens for r in results),
        completion_tokens=sum(r.completion_tokens for r in results),
        tokens_per_resolved=None if resolved == 0 else tokens / resolved,
        cost_usd=cost, cost_per_resolved=None if resolved == 0 else cost / resolved,
        unauthorized_attempts=sum(r.unauthorized_attempts for r in results),
        runs_with_test_edits=sum(r.test_files_modified for r in results),
        failure_reasons=dict(reasons.most_common()), by_difficulty=by_difficulty,
    )


def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate * 100:.1f}%"


def _num(value: float | None, fmt: str = "{:,.0f}") -> str:
    return "n/a" if value is None else fmt.format(value)


DEFINITIONS = """\
- **Resolved**: the run passed harness validation (non-empty diff, no protected paths touched,
  test suite green) *and* the task's held-out tests pass on the final workspace.
- **Patch applied**: the final diff is non-empty. **Visible tests passed**: the last
  validation passed. **Baseline**: the visible suite already passed on the untouched buggy
  code, which is true for every task here, so visible-test success alone proves little.
- **Tool-call failure rate**: failed tool calls / tool calls. Policy denials count as failures
  and also as unauthorized attempts. Control tools (submit_plan, finish) are not tool calls.
- **Latency**: wall clock per task, sandbox start to grading; p50/p95 are nearest-rank.
- **Tokens / cost per resolved task**: totals over *all* tasks divided by resolved tasks.
- Counts are recorded per agent step; if a run crashes mid-step and resumes, that step's
  tool calls may be counted twice (at-least-once).
- Confidence intervals are 95% Wilson score intervals.
"""


def render_markdown(name: str, meta: dict, summary: Summary, results: list[EvalResult]) -> str:
    s = summary
    lo, hi = s.resolution_ci
    ci = "" if lo is None else f" (95% CI {lo * 100:.0f}–{hi * 100:.0f}%)"
    lines = [
        f"# Eval `{name}`", "",
        " · ".join(f"**{k}**: {v}" for k, v in meta.items()), "",
        "| Metric | Value |", "|---|---|",
        f"| Resolution rate | {_pct(s.resolution_rate)} ({s.resolved}/{s.n}) |",
        f"| Resolution 95% CI | {ci.strip(' ()') or 'n/a'} |",
        f"| Patch-apply rate | {_pct(s.patch_applied_rate)} |",
        f"| Visible-test pass rate | {_pct(s.visible_pass_rate)} (baseline {_pct(s.baseline_pass_rate)}) |",
        f"| Tool-call failure rate | {_pct(s.tool_failure_rate)} of {s.tool_calls} calls |",
        f"| Average retries | {_num(s.avg_retries, '{:.2f}')} |",
        f"| Latency p50 / p95 | {_num(s.p50_latency_s, '{:.0f}')} s / {_num(s.p95_latency_s, '{:.0f}')} s |",
        f"| Tokens (prompt + completion) | {s.prompt_tokens:,} + {s.completion_tokens:,} |",
        f"| Tokens per resolved task | {_num(s.tokens_per_resolved)} |",
        f"| Cost / per resolved task | ${s.cost_usd:.4f} / {_num(s.cost_per_resolved, '${:.4f}')} |",
        f"| Unauthorized modification attempts | {s.unauthorized_attempts} |",
        f"| Runs that edited existing tests | {s.runs_with_test_edits} |",
        f"| Harness errors | {s.errors} |",
        "",
    ]
    if s.by_difficulty:
        lines += ["## By difficulty", "", "| Difficulty | Resolved | Rate |", "|---|---|---|"]
        lines += [f"| {k} | {v.resolved}/{v.n} | {_pct(v.resolution_rate)} |"
                  for k, v in s.by_difficulty.items()]
        lines.append("")
    if s.failure_reasons:
        lines += ["## Unresolved, by reason", "", "| Reason | Tasks |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in s.failure_reasons.items()]
        lines.append("")
    lines += ["## Tasks", "",
              "| Task | Difficulty | Resolved | Status | Reason | Retries | LLM / tool calls | Tokens | Time |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(results, key=lambda r: r.task_id):
        reason = r.failure_reason or (r.error or "")[:60]
        lines.append(
            f"| {r.task_id} | {r.difficulty} | {'yes' if r.resolved else 'no'} | {r.status} | "
            f"{reason} | {r.retries} | {r.llm_calls} / {r.tool_calls} | "
            f"{r.prompt_tokens + r.completion_tokens:,} | {r.latency_ms / 1000:.0f} s |")
    lines += ["", "## Definitions", "", DEFINITIONS]
    return "\n".join(lines)
