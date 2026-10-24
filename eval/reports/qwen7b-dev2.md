# Eval `qwen7b-dev2`

**model**: qwen2.5-7b-16k · **endpoint**: http://127.0.0.1:11434/v1 · **tasks**: 15 · **git_sha**: 4be50bc · **date**: 2026-10-21 · **host**: Windows-11 · **budget**: max_llm_calls=40, max_tool_calls=80, max_seconds=2400.0, max_retries=3

| Metric | Value |
|---|---|
| Resolution rate | 33.3% (5/15) |
| Resolution 95% CI | 15–58% |
| Patch-apply rate | 40.0% |
| Visible-test pass rate | 40.0% (baseline 100.0%) |
| Tool-call failure rate | 12.7% of 166 calls |
| Average retries | 0.07 |
| Latency p50 / p95 | 311 s / 4610 s |
| Tokens (prompt + completion) | 587,129 + 27,070 |
| Tokens per resolved task | 122,840 |
| Cost / per resolved task | $0.0000 / $0.0000 |
| Unauthorized modification attempts | 0 |
| Runs that edited existing tests | 0 |
| Harness errors | 0 |

## By difficulty

| Difficulty | Resolved | Rate |
|---|---|---|
| complex | 0/3 | 0.0% |
| simple | 5/12 | 41.7% |

## Unresolved, by reason

| Reason | Tasks |
|---|---|
| no_tool_calls | 6 |
| budget:seconds | 3 |
| completed | 1 |

## Tasks

| Task | Difficulty | Resolved | Status | Reason | Retries | LLM / tool calls | Tokens | Time |
|---|---|---|---|---|---|---|---|---|
| inventory-001 | complex | no | failed | budget:seconds | 0 | 32 / 24 | 159,955 | 4610 s |
| inventory-002 | simple | no | failed | no_tool_calls | 0 | 18 / 8 | 45,018 | 843 s |
| inventory-003 | simple | yes | completed |  | 0 | 9 / 7 | 16,305 | 176 s |
| inventory-004 | simple | yes | completed |  | 0 | 9 / 7 | 15,247 | 159 s |
| orders-api-001 | simple | yes | completed |  | 0 | 20 / 17 | 41,586 | 293 s |
| orders-api-002 | simple | no | completed |  | 0 | 11 / 9 | 21,633 | 178 s |
| orders-api-003 | simple | no | failed | no_tool_calls | 0 | 20 / 14 | 39,754 | 288 s |
| orders-api-004 | simple | no | failed | no_tool_calls | 0 | 12 / 7 | 30,002 | 326 s |
| orders-api-005 | simple | yes | completed |  | 0 | 6 / 4 | 10,941 | 97 s |
| orders-api-006 | simple | no | failed | budget:seconds | 1 | 14 / 11 | 41,334 | 2827 s |
| textkit-001 | simple | no | failed | no_tool_calls | 0 | 9 / 4 | 17,436 | 311 s |
| textkit-002 | simple | yes | completed |  | 0 | 12 / 10 | 25,717 | 170 s |
| textkit-003 | complex | no | failed | no_tool_calls | 0 | 26 / 19 | 61,952 | 719 s |
| textkit-004 | simple | no | failed | no_tool_calls | 0 | 22 / 15 | 49,674 | 512 s |
| textkit-005 | complex | no | failed | budget:seconds | 0 | 14 / 10 | 37,645 | 4345 s |

## Definitions

- **Resolved**: the run passed harness validation (non-empty diff, no protected paths touched,
  test suite green) *and* the task's held-out tests pass on the final workspace.
- **Patch applied**: the final diff is non-empty. **Visible tests passed**: the last
  validation passed. **Baseline**: the visible suite already passed on the untouched buggy
  code, which is true for every task here, so visible-test success alone proves little.
- **Tool-call failure rate**: failed tool calls / tool calls. Policy denials count as failures
  and also as unauthorized attempts. Control tools (submit_plan, finish) are not tool calls.
- **Latency**: wall clock per task, sandbox start to grading; p50/p95 are nearest-rank.
- **Tokens / cost per resolved task**: totals over *all* tasks divided by resolved tasks.
  Ollama reports prompt tokens without the KV-cached prefix, so local-model prompt totals are
  undercounts; hosted APIs report full prompt tokens.
- Counts are recorded per agent step; if a run crashes mid-step and resumes, that step's
  tool calls may be counted twice (at-least-once).
- Confidence intervals are 95% Wilson score intervals.
