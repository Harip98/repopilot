# Evaluation

RepoPilot is measured on a purpose-built benchmark. The scoring is deterministic: no model
grades another model's work. This page covers what is measured, the results, why runs fail, and
how to reproduce them.

## What "resolved" means

A task is **resolved** when both of these are true:

1. The run passed the harness's own validation: no protected file changed and the task's test
   command passed.
2. The task's **held-out tests**, which the agent never sees, pass on the agent's final
   workspace.

## The benchmark

The benchmark has 25 tasks, each a bug injected into one of three small, realistic fixture
repositories:

| Repo | Tasks | What it is |
|---|---|---|
| `orders-api` | 10 | FastAPI orders service: pagination, money and status rules |
| `textkit` | 8 | Text utilities: slugs, truncation, CSV, identifier case |
| `inventory` | 7 | FIFO cost ledger, reorder points, CSV import |

Every task has:

- an **issue** written the way a user would report it: symptoms, not file names;
- a **reference fix**;
- **held-out tests** that fail on the bug and pass with the reference fix.

Several rules keep the benchmark from being gamed:

- **The visible tests already pass on the buggy code**, so the agent has to reproduce the bug
  from the issue. "Make the failing test pass" is not available.
- **The fix is not in `.git`.** The workspace is a single import commit with the bug already
  applied.
- **The held-out tests are protected.** Their names, `conftest.py` and the pytest configuration
  are protected paths, so the agent cannot edit them.
- **Planted pytest hooks don't work.** Scoring copies a pristine `tests/conftest.py` over the
  agent's before running the held-out tests, so a planted hook cannot force a pass.
- **Every task is proven sound.** `repopilot bench validate` checks each one: the bug applies,
  the held-out tests fail on it, and the reference fix makes them pass.

## Dev and test splits

The 25 tasks are split the way machine-learning data is:

- **Dev (15 tasks)** is for tuning. The agent was run on it, its failures were studied, and the
  prompts and settings were improved.
- **Test (10 tasks)** is the final exam. It was never looked at while tuning, and it was run
  **once per model** after all tuning was done.

Before the test run, the test split was **frozen**: each task's files and the sandbox images
were fingerprinted ([`bench/test_split.sha256`](../bench/test_split.sha256), logged in
[`bench/FREEZE_LOG.md`](../bench/FREEZE_LOG.md)). `eval run` refuses to run the test split if
any of them has changed since.

## Results

| Run | Model | Resolved | 95% CI | Tool-call failures | Median time | Notes |
|---|---|---|---|---|---|---|
| dev v1 | qwen2.5 7B (local, Ollama) | 4 / 15 (26.7%) | 11–52% | 3.3% of 122 | 185 s | Before tuning: 6 runs ended on unparseable tool calls. The laptop slept during orders-api-006, so its 23,049 s (the p95 in the report) and its time-budget failure are not agent behaviour |
| dev v2 | qwen2.5 7B (local, Ollama) | 5 / 15 (33.3%) | 15–58% | 12.7% of 166 | 311 s | After tuning (consecutive nudges, 2400 s budget). Still 6 runs lost to tool calls Ollama could not parse: the model emitted ~200 tokens of call that came back empty |
| **test** | qwen2.5 7B (local, Ollama) | **1 / 10 (10.0%)** | 2–40% | 25.3% of 190 | 443 s | Frozen split, run once. Unresolved: 5 unparseable tool calls, 2 hit the 40-call budget, 2 the time budget |
| test, baseline | qwen2.5 3B (local, Ollama) | 0 / 10 (0.0%) | 0–28% | 28.1% of 128 | 57 s | Same budget. Unresolved: 6 unparseable tool calls, 2 call budget, 1 no change, 1 failed the hidden tests |

The confidence intervals are 95% Wilson intervals. Each run's full report, with per-task
outcomes, failure reasons, token use and policy denials, is in
[`eval/reports/`](../eval/reports/).

## What the numbers say

**The harness held.** Across all 50 measured runs there were:

- 0 harness errors;
- 0 attempts to modify protected files;
- 0 edits to existing tests.

Every "resolved" was decided by hidden tests the model never saw.

**The model is the bottleneck.** Most runs end before the model produces a fix at all, so the
hidden tests never run. When the 7B model did produce a fix that passed validation, the hidden
tests confirmed it 10 times out of 13:

| Where the 40 runs of the 7B model ended | Runs |
|---|---|
| Produced a validated fix | 13 (10 resolved, 3 failed the hidden tests) |
| No usable tool call even after three nudges (`no_tool_calls`) | 17 |
| Out of time (`budget:seconds`) | 8 |
| Out of model calls (`budget:llm_calls`) | 2 |

There are two main causes:

- **Malformed tool calls.** A 7B model, quantized to fit a 4 GB GPU, often emits a tool call
  that Ollama cannot parse. Ollama then returns an empty reply. The agent nudges the model up
  to three times in a row, and a fourth reply without a tool call fails the run.
- **Slow hardware.** With the model running mostly on the CPU, one model call can take minutes.
  inventory-005 made only 5 model calls in 4,394 s and hit the time budget.

**Dev and test differ.** Dev (33%) and test (10%) differ more than the task mix explains:

- dev was used for tuning;
- dev ran on an earlier benchmark revision (see below);
- with 10 and 15 tasks, the intervals overlap widely.

Read both as "a small local model resolves a minority of these issues". The obvious next run is
a hosted model, which needs a configuration change, not a code change.

## Read these numbers with their limits

- **The model is small.** It is a 7B model on a 4 GB laptop GPU. A hosted model only needs
  `REPOPILOT_LLM_BASE_URL`, `REPOPILOT_LLM_MODEL` and `REPOPILOT_LLM_API_KEY` set; the harness
  stays the same.
- **Dev v1 and v2 ran on an earlier revision of the benchmark.** Commit `151cf98`, made after
  them and before the test split was frozen, made three changes:
  - it removed code comments that hinted at fixes, such as "oldest first (FIFO)" in the
    docstring of the inventory ledger's `ship()`, which dev task inventory-002 breaks;
  - it re-derived each task's difficulty from the size of its fix;
  - it added `.pytest.ini` to the protected paths.

  The test runs used the revised benchmark.
- **The dev reports were rendered by older report code.** They list held-out failures under
  `completed` (now `heldout_failed`). They also count a patch as applied when the final diff is
  non-empty; the test reports count it when the workspace differs from the imported code.
  Neither figure is used above.
- **The fixtures are small.** Every reference fix is one hunk in one file (*complex* means it
  changes 3 or more lines).
- **Local token counts are low.** Ollama reports prompt tokens without its cached prefix.
- **Latency is hardware-bound.** The wall-clock budget is checked between steps, so one slow
  model call can overshoot it: inventory-005 took 4,394 s against a 2,400 s budget.

## Changes since the reported runs

The reported runs were made at git `5179422`. A later audit found and fixed a bug in the
agent's tool gate. On the 15th exploration reply, the tools the model had just been offered
were refused. The tools of the last allowed model call were also refused, so that call was
wasted. Some of the measured runs were affected.

The numbers above are kept exactly as measured. A re-run on the current code may differ, and it
would be reported as a new eval name next to these rows, not in place of them.

## Reproduce

From `services/agent-svc`, with the stack running and the sandbox images built (see
[running.md](running.md)):

```bash
uv run repopilot bench validate                         # all 25 tasks are sound
uv run repopilot eval run --name my-dev --split dev --max-seconds 2400
uv run repopilot eval run --name my-test --split test --max-seconds 2400
uv run repopilot eval compare my-dev my-test
```

`--max-seconds 2400` is the per-task wall-clock budget that every reported run used except dev
v1, which used the 1200 s default (see `eval/reports/*.meta.json`). The longer budget is for a
7B model running mostly on the CPU.

Use a new `--name` for every run. The test split refuses to run if any frozen task has changed.
