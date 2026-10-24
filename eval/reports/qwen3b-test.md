# Eval `qwen3b-test`

**model**: qwen2.5-3b-16k · **endpoint**: http://127.0.0.1:11434/v1 · **split**: test · **tasks**: 10 · **task_hash**: db49406366504b0c · **git_sha**: 5179422 · **date**: 2026-10-21 · **host**: Windows-11 · **budget**: max_llm_calls=40, max_tool_calls=80, max_tokens=600000, max_seconds=2400.0, max_retries=3, max_explore_steps=15

| Metric | Value |
|---|---|
| Resolution rate | 0.0% (0/10) |
| Resolution 95% CI | 0–28% |
| Patch-apply rate | 30.0% |
| Visible-test pass rate | 10.0% (baseline 100.0%) |
| Tool-call failure rate | 28.1% of 128 calls |
| Average retries | 0.50 |
| Latency p50 / p95 | 57 s / 135 s |
| Tokens (prompt + completion) | 656,191 + 17,773 |
| Tokens per resolved task | n/a |
| Cost / per resolved task | $0.0000 / n/a |
| Unauthorized modification attempts | 0 |
| Runs that edited existing tests | 0 |
| Harness errors | 0 |

## By difficulty

| Difficulty | Resolved | Rate |
|---|---|---|
| complex | 0/3 | 0.0% |
| simple | 0/7 | 0.0% |

## Unresolved, by reason

| Reason | Tasks |
|---|---|
| no_tool_calls | 6 |
| budget:llm_calls | 2 |
| no_changes | 1 |
| heldout_failed | 1 |

## Tasks

| Task | Difficulty | Resolved | Status | Reason | Retries | LLM / tool calls | Tokens | Time |
|---|---|---|---|---|---|---|---|---|
| inventory-005 | simple | no | failed | budget:llm_calls | 0 | 40 / 22 | 181,766 | 135 s |
| inventory-006 | simple | no | failed | no_tool_calls | 0 | 11 / 5 | 20,393 | 37 s |
| inventory-007 | simple | no | failed | budget:llm_calls | 0 | 40 / 19 | 163,151 | 103 s |
| orders-api-007 | simple | no | failed | no_tool_calls | 2 | 11 / 8 | 32,828 | 63 s |
| orders-api-008 | complex | no | failed | no_tool_calls | 0 | 18 / 18 | 36,034 | 50 s |
| orders-api-009 | simple | no | failed | no_changes | 3 | 18 / 22 | 58,534 | 116 s |
| orders-api-010 | simple | no | failed | no_tool_calls | 0 | 19 / 10 | 50,678 | 57 s |
| textkit-006 | complex | no | completed |  | 0 | 7 / 3 | 10,161 | 19 s |
| textkit-007 | complex | no | failed | no_tool_calls | 0 | 30 / 14 | 86,903 | 79 s |
| textkit-008 | simple | no | failed | no_tool_calls | 0 | 16 / 7 | 33,516 | 46 s |

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
