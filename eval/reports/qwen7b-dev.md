# Eval `qwen7b-dev`

**model**: qwen2.5-7b-16k · **endpoint**: http://127.0.0.1:11434/v1 · **tasks**: 15 · **git_sha**: 74b3a3b · **date**: 2026-10-20 · **host**: Windows-11 · **budget**: max_llm_calls=40, max_tool_calls=80, max_seconds=1200.0, max_retries=3

| Metric | Value |
|---|---|
| Resolution rate | 26.7% (4/15) |
| Resolution 95% CI | 11–52% |
| Patch-apply rate | 40.0% |
| Visible-test pass rate | 40.0% (baseline 100.0%) |
| Tool-call failure rate | 3.3% of 122 calls |
| Average retries | 0.00 |
| Latency p50 / p95 | 185 s / 23049 s |
| Tokens (prompt + completion) | 321,794 + 11,725 |
| Tokens per resolved task | 83,380 |
| Cost / per resolved task | $0.0000 / $0.0000 |
| Unauthorized modification attempts | 0 |
| Runs that edited existing tests | 0 |
| Harness errors | 0 |

## By difficulty

| Difficulty | Resolved | Rate |
|---|---|---|
| complex | 0/3 | 0.0% |
| simple | 4/12 | 33.3% |

## Unresolved, by reason

| Reason | Tasks |
|---|---|
| no_tool_calls | 6 |
| budget:seconds | 3 |
| completed | 2 |

## Tasks

| Task | Difficulty | Resolved | Status | Reason | Retries | LLM / tool calls | Tokens | Time |
|---|---|---|---|---|---|---|---|---|
| inventory-001 | complex | no | failed | no_tool_calls | 0 | 9 / 5 | 21,668 | 242 s |
| inventory-002 | simple | no | failed | no_tool_calls | 0 | 10 / 6 | 18,327 | 101 s |
| inventory-003 | simple | yes | completed |  | 0 | 9 / 7 | 16,236 | 100 s |
| inventory-004 | simple | yes | completed |  | 0 | 9 / 7 | 15,248 | 115 s |
| orders-api-001 | simple | yes | completed |  | 0 | 20 / 17 | 41,595 | 197 s |
| orders-api-002 | simple | no | completed |  | 0 | 11 / 9 | 21,633 | 115 s |
| orders-api-003 | simple | no | failed | no_tool_calls | 0 | 20 / 15 | 39,818 | 185 s |
| orders-api-004 | simple | no | failed | no_tool_calls | 0 | 14 / 10 | 38,501 | 251 s |
| orders-api-005 | simple | yes | completed |  | 0 | 6 / 4 | 10,949 | 64 s |
| orders-api-006 | simple | no | failed | budget:seconds | 0 | 7 / 6 | 11,182 | 23049 s |
| textkit-001 | simple | no | failed | no_tool_calls | 0 | 9 / 5 | 16,674 | 165 s |
| textkit-002 | simple | no | failed | budget:seconds | 0 | 9 / 7 | 17,307 | 4219 s |
| textkit-003 | complex | no | completed |  | 0 | 10 / 8 | 19,285 | 131 s |
| textkit-004 | simple | no | failed | no_tool_calls | 0 | 13 / 9 | 27,245 | 375 s |
| textkit-005 | complex | no | failed | budget:seconds | 0 | 9 / 7 | 17,851 | 2204 s |

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
