# Eval `qwen7b-test`

**model**: qwen2.5-7b-16k · **endpoint**: http://127.0.0.1:11434/v1 · **split**: test · **tasks**: 10 · **task_hash**: db49406366504b0c · **git_sha**: 42f8d63 · **date**: 2026-10-21 · **host**: Windows-11 · **budget**: max_llm_calls=40, max_tool_calls=80, max_tokens=600000, max_seconds=2400.0, max_retries=3, max_explore_steps=15

| Metric | Value |
|---|---|
| Resolution rate | 10.0% (1/10) |
| Resolution 95% CI | 2–40% |
| Patch-apply rate | 50.0% |
| Visible-test pass rate | 10.0% (baseline 100.0%) |
| Tool-call failure rate | 25.3% of 190 calls |
| Average retries | 0.20 |
| Latency p50 / p95 | 443 s / 4394 s |
| Tokens (prompt + completion) | 794,894 + 23,559 |
| Tokens per resolved task | 818,453 |
| Cost / per resolved task | $0.0000 / $0.0000 |
| Unauthorized modification attempts | 0 |
| Runs that edited existing tests | 0 |
| Harness errors | 0 |

## By difficulty

| Difficulty | Resolved | Rate |
|---|---|---|
| complex | 0/3 | 0.0% |
| simple | 1/7 | 14.3% |

## Unresolved, by reason

| Reason | Tasks |
|---|---|
| no_tool_calls | 5 |
| budget:seconds | 2 |
| budget:llm_calls | 2 |

## Tasks

| Task | Difficulty | Resolved | Status | Reason | Retries | LLM / tool calls | Tokens | Time |
|---|---|---|---|---|---|---|---|---|
| inventory-005 | simple | no | failed | budget:seconds | 0 | 5 / 4 | 6,759 | 4394 s |
| inventory-006 | simple | no | failed | no_tool_calls | 0 | 19 / 14 | 28,180 | 157 s |
| inventory-007 | simple | yes | completed |  | 0 | 21 / 18 | 74,410 | 443 s |
| orders-api-007 | simple | no | failed | no_tool_calls | 0 | 19 / 14 | 31,417 | 145 s |
| orders-api-008 | complex | no | failed | budget:llm_calls | 0 | 40 / 34 | 178,089 | 836 s |
| orders-api-009 | simple | no | failed | budget:llm_calls | 0 | 40 / 35 | 135,880 | 1285 s |
| orders-api-010 | simple | no | failed | no_tool_calls | 0 | 25 / 18 | 59,370 | 350 s |
| textkit-006 | complex | no | failed | no_tool_calls | 0 | 4 / 0 | 4,814 | 30 s |
| textkit-007 | complex | no | failed | budget:seconds | 0 | 35 / 33 | 177,934 | 2440 s |
| textkit-008 | simple | no | failed | no_tool_calls | 2 | 33 / 20 | 121,600 | 750 s |

## Definitions

- **Resolved**: the run passed harness validation (non-empty diff, no protected paths touched,
  test suite green) *and* the task's held-out tests pass on the final workspace.
- **Patch applied**: the workspace differs from the imported code at the end of the run
  (read before grading, so it counts runs that edited and then stalled or ran out of budget).
  **Visible tests passed**: the last
  validation passed. **Baseline**: the visible suite already passed on the untouched buggy
  code, which is true for every task here, so visible-test success alone proves little.
- **Harness errors**: the run died on the LLM endpoint or the sandbox, or the harness
  raised. Counted in n as unresolved, and retried when the eval is resumed.
- **Difficulty**: every reference fix is one hunk in one file; *complex* means it changes
  3 or more lines.
- **Tool-call failure rate**: failed tool calls / tool calls. Policy denials count as failures
  and also as unauthorized attempts. Control tools (submit_plan, finish) are not tool calls.
- **Latency**: wall clock per task, sandbox start to grading; p50/p95 are nearest-rank and
  leave out harness errors, which carry no usage either.
- **Tokens / cost per resolved task**: totals over *all* tasks divided by resolved tasks.
  Ollama reports prompt tokens without the KV-cached prefix, so local-model prompt totals are
  undercounts; hosted APIs report full prompt tokens.
- Counts are recorded per agent step; if a run crashes mid-step and resumes, that step's
  tool calls may be counted twice (at-least-once).
- Confidence intervals are 95% Wilson score intervals.
