# M3 LangGraph Agent Implementation Plan

> The plan as written before implementation, kept as design history; its checkboxes were not used for tracking. [README.md](../../README.md) and [architecture.md](../architecture.md) describe the system as built.

**Goal:** Build the agent itself, a LangGraph state machine. It:
- investigates an issue with read-only tools and submits a plan
- edits with write tools
- is validated by the harness, not by its own claim
- gets bounded retries from test output
- stops at a human approval interrupt that survives process restarts
- has every step recorded

It runs end to end on a benchmark task via `repopilot solve` and `repopilot approve`.

**Architecture:**
- **Graph shape.** One LLM turn per `llm` node execution, and that turn's tool calls per `tools` node execution. The graph checkpoints after every step, so a crash resumes mid-investigation rather than restarting.
- **Phases.** `phase` in the state selects the tool set:
  - `explore`: read-only tools plus the control tool `submit_plan`
  - `edit`: all tools plus `finish`
- **Validation.** `validate` is deterministic. It runs the task's test command in the sandbox and checks the diff against policy. It never trusts the model.
- **Dependencies.** The LLM, sandbox client, tool registry, recorder and PR opener are passed as LangGraph runtime `context` (a `RunDeps` dataclass). They are never serialized into checkpoints.
- **Checkpointing.** `AsyncPostgresSaver` in production, `InMemorySaver` in tests.

**Tech Stack:** `langgraph>=1.2,<2` and `langgraph-checkpoint-postgres>=3.2,<4`. Both were verified in a spike on 2026-10-21: Pydantic state, `interrupt()`/`Command(resume=)` across graph instances, and `context_schema`. Also M2's LLM layer, tools and bench, and psycopg 3.

**Spec:** [docs/plan.md](../plan.md), sections Architecture (Graph) and M3.

## Global Constraints

- **Graph nodes and routes** (exact):

  | Node | Next |
  |---|---|
  | `START` | `intake` |
  | `intake` | `llm` |
  | `llm` | `tools` if the reply has tool calls; `nudge` if there are no tool calls and `nudges < 2`; `fail` if over budget or out of nudges |
  | `nudge` | `llm` |
  | `tools` | `llm` (in edit phase, after `finish`, it goes to `validate`); `submit_plan` switches the phase to `edit` |
  | `validate` | `approval` if passed; `diagnose` if failed and `retries < max_retries`; `fail` otherwise |
  | `diagnose` | `llm` |
  | `approval` | `open_pr` on approve; `llm` on reject with a comment while `revisions < 1`; `record` otherwise |
  | `open_pr` | `record` |
  | `fail` | `record` |
  | `record` | `END` |

- **Budgets** (`Budget` model, defaults): `max_llm_calls=40`, `max_tool_calls=80`, `max_tokens=600_000` (prompt + completion), `max_seconds=1200`, `max_retries=3`, `max_explore_steps=15`.
  - Exceeding any budget routes to `fail` with `failure_reason="budget:<name>"`.
  - After `max_explore_steps` LLM calls in explore, the next LLM call gets only `submit_plan` (forced plan).
- **Control tools.** These are handled by the graph, not by the registry:
  - `submit_plan(root_cause: str, files: list[str], acceptance_criteria: list[str], test_targets: list[str])`
  - `finish(summary: str)`

  They answer the model with "plan recorded" or "submitted for validation". Calling a control tool in the wrong phase gets an error tool message.
- **Every tool call is recorded** with tool, args, ok, denied, error, latency_ms and step. Denials count towards `unauthorized_attempts`.
- **Validation** passes only when all three hold:
  1. `git diff` (with intent-to-add) is non-empty;
  2. every changed path passes `ToolPolicy.check_write`;
  3. `test_command` exits 0 within 300 s.

  The test output tail (≤ 4000 chars) goes into `diagnose`'s message to the model.
- **The approval interrupt payload** is `{"run_id", "diff", "tests": {"exit_code", "output_tail"}, "plan", "summary"}`. The resume value is `{"decision": "approve"|"reject", "comment": str|None, "by": str}`. In eval mode, `auto_approve=True` skips the interrupt and records `by="auto"`.
- **Run statuses** written to `runs.status`: `running` → `awaiting_approval` → (`approved` → `pr_opened` | `completed`) | `rejected` | `failed`. Migration `002_run_outcomes.sql` adds `completed` (approved, no PR to open) to the CHECK, and adds the columns `failure_reason text`, `llm_calls int`, `tool_calls int`, `tool_failures int`, `unauthorized_attempts int`, `task_mode text`.
- **The system prompt** says, among the other rules: never edit tests just to make them pass; you may add tests; protected paths are off limits; finish only when you believe the fix is complete.
- **The model is never shown** held-out tests or the task's `expected_files`.

## Review Focus

1. **The model answers with plain text and no tool calls, repeatedly.** Two nudges, then `fail` with `no_tool_calls`; no infinite loop. Test: `test_text_only_replies_fail_after_nudges` (Task 2).
2. **The process dies between `tools` and the next `llm` step.** A new process with the same checkpointer and thread resumes at `llm` and completes, without repeating already-recorded tool calls. Test: `test_resume_after_crash` (Task 2).
3. **The model calls `finish` before making any change.** `validate` fails with an empty diff, goes to `diagnose`, and the model is told "no changes". Test: `test_finish_without_changes` (Task 2).
4. **The model edits a protected file through a path the policy missed**, for example a file created by the test run. `validate` rejects changed protected paths. Test: `test_validate_rejects_protected_changes` (Task 2).
5. **`approve` runs for an unknown run, or one that is not awaiting approval.** It gives a clear error and exit code 1, and no graph resume. Test: `test_approve_rejects_wrong_state` (Task 4).

---

### Task 1: State, prompts, control tools, budgets

**Files:**
- Create: `src/repopilot/agent/__init__.py`, `agent/state.py`, `agent/prompts.py`, `agent/control.py`
- Test: `tests/agent/test_state.py`

**Interfaces:**
- Produces:
  - `Plan(root_cause, files, acceptance_criteria, test_targets)`
  - `Budget` (fields above)
  - `Usage` totals: `llm_calls`, `tool_calls`, `tool_failures`, `unauthorized_attempts`, `prompt_tokens`, `completion_tokens`, `cost_usd`
  - `RunState`, a Pydantic model with these fields:
    - `run_id`, `task_id: str | None`, `repo`, `issue_title`, `issue_text`, `sandbox_id`, `test_command`, `protected_paths`
    - `phase: Literal["explore","edit"]`, `messages: list[dict]`, `plan: Plan | None`, `summary: str | None`
    - `retries`, `nudges`, `revisions`, `explore_steps`
    - `last_tests: TestReport | None`, `diff: str`
    - `usage: Usage`, `budget: Budget`, `started_at: float`
    - `approval: Approval | None`, `status`, `failure_reason: str | None`, `pr_url: str | None`
    - `auto_approve: bool`
  - `budget_exceeded(state, now) -> str | None`
  - `system_prompt(state) -> str`, `issue_message(state) -> dict`
  - `SUBMIT_PLAN`/`FINISH: ToolSpec`
  - `tools_for(state, registry) -> list[ToolSpec]`

Steps:
- [ ] Write failing tests:
  - `test_budget_exceeded_each_limit`
  - `test_tools_for_phases`: explore gives read-only specs plus `submit_plan`; edit gives all specs plus `finish`; a forced plan gives only `submit_plan`
  - `test_system_prompt_mentions_rules_not_secrets`: has "never edit tests just to make them pass"; contains no `expected_files` or heldout path
  - `test_state_roundtrips_json`
- [ ] Implement, run `uv run pytest tests/agent -q`, commit `feat(agent): run state, budgets, prompts and control tools`.

### Task 2: Graph nodes and builder

**Files:**
- Create: `agent/deps.py` (`RunDeps` dataclass: `llm: ChatModel`, `sandbox`, `registry: ToolRegistry`, `recorder: Recorder`, `pr: PullRequestOpener`, `price_in`, `price_out`, `clock`), `agent/nodes.py`, `agent/graph.py`, `agent/recorder.py` (`Recorder` protocol and `MemoryRecorder`), `agent/pr.py` (`PullRequestOpener` protocol and `NoPR`)
- Test: `tests/agent/test_graph.py`, using `FakeChatModel` and the `FakeSandbox` from M2 tests (moved to `tests/fakes.py`)

**Interfaces:**
- Consumes: Task 1; M2 `ToolRegistry.dispatch`, `ToolOutcome.for_model`, `cost_usd`.
- Produces:
  - `build_graph() -> StateGraph`, uncompiled, with `context_schema=RunDeps`
  - `Recorder` protocol, with async methods:
    - `event(run_id, node, kind, payload)`
    - `tool_call(run_id, step, call: ToolCall, outcome: ToolOutcome)`
    - `status(run_id, status, **fields)`
  - `PullRequestOpener.open(state) -> str | None`

Steps:
- [ ] Write failing tests. Each scripts the fake LLM, runs `build_graph().compile(checkpointer=InMemorySaver())`, and asserts on the final state and the recorder:
  - `test_happy_path`: explore `read_file` → `submit_plan` → `replace_text` → `finish` → validate passes → interrupt payload → resume approve → `NoPR` → status `completed`
  - `test_test_failure_diagnose_then_fix`: the first validate fails, the diagnose message contains the output tail, the second validate passes, and `retries == 1`
  - `test_retries_exhausted_fails`: `failure_reason == "tests_failing"`
  - `test_budget_exhausted`: `max_llm_calls=3` gives `failure_reason == "budget:llm_calls"`
  - `test_policy_violation_counted`: an `apply_patch` to `.github/x` gives `unauthorized_attempts == 1` and the run continues
  - `test_text_only_replies_fail_after_nudges`
  - `test_finish_without_changes`
  - `test_validate_rejects_protected_changes`
  - `test_reject_with_comment_revises_once`: reject with a comment gives one more edit round; a second reject gives `rejected`
  - `test_resume_after_crash`: the fake LLM raises on call 4; a new compiled graph on the same saver with a fresh script resumes and completes; the recorder holds no duplicate tool calls for steps before the crash
  - `test_auto_approve_skips_interrupt`
- [ ] Implement the nodes:
  - **`llm`:** checks the budget, picks the tools, calls the model, appends the assistant message, updates usage, and records an event.
  - **`tools`:** dispatches each call in order (control tools inline), appends tool messages, and records each call.
  - **`validate`:** runs `git add --intent-to-add --all`, `git diff`, `git diff --name-only`, then `test_command`.
  - **`diagnose`:** appends a user message with the failure.
  - **`approval`:** `interrupt` unless `auto_approve`.
  - **`open_pr`**, **`fail`** and **`record`**: final status, with usage and cost.
- [ ] Run the tests until PASS, then commit `feat(agent): LangGraph agent with validation, bounded retries and approval interrupt`.

### Task 3: Postgres recorder and migration 002

**Files:**
- Create: `db/migrations/002_run_outcomes.sql`, `agent/pg_recorder.py` (`PgRecorder(conninfo)` on `psycopg.AsyncConnection`)
- Test: `tests/integration/test_pg_recorder.py`

Steps:
- [ ] Write failing integration tests:
  - `test_status_lifecycle`: running → awaiting_approval → completed, with usage columns written
  - `test_events_sequenced`: `seq` increases per run, and concurrent writers are serialized by `SELECT … FOR UPDATE` on the run row
  - `test_tool_calls_recorded`: args are stored as jsonb and `result_excerpt` is capped at 2000 chars
  - `test_completed_status_allowed`
- [ ] Implement and migrate. Run `uv run pytest -m integration -q`, then commit `feat(agent): Postgres run recorder and run outcome columns`.

### Task 4: Runner and CLI (`solve`, `approve`, `runs`)

**Files:**
- Create: `agent/runner.py`
- Modify: `cli.py`
- Test: `tests/agent/test_runner.py` (fakes); `tests/sandbox/test_solve_live.py` (marker `sandbox`, needs compose Postgres too)

**Interfaces:**
- Produces:
  - `prepare_bench_run(task, sandbox) -> (sandbox_id, RunState)`: builds the base workspace, creates a sandbox from `image_for(task)`, uploads, and returns the initial state
  - `async start_run(state, deps, checkpointer) -> RunState`: runs until an interrupt or the end
  - `async resume_run(run_id, decision, deps, checkpointer) -> RunState`
  - `async grade(task, sandbox, sandbox_id) -> bool`: uploads the held-out tests and runs `heldout_command`; used by M4
- CLI:
  - `repopilot solve --task ID [--model M] [--auto-approve]` prints the run id, plan, diff and test summary
  - `repopilot approve RUN_ID [--reject] [--comment TEXT]`
  - `repopilot runs` lists recent runs from Postgres

Steps:
- [ ] Write failing tests:
  - `test_start_then_resume_across_graph_instances` (in-memory saver)
  - `test_approve_rejects_wrong_state`
  - live `test_solve_scripted_orders_api_005`: a scripted fake LLM applies the task's reference fix through `apply_patch` against the real sandbox and real Postgres checkpointer → validate passes → auto-approve → `grade` gives True; with the bug left in place, `grade` gives False
- [ ] Implement:
  - `AsyncPostgresSaver.from_conn_string(settings.dsn())` with `await saver.setup()` once
  - the sandbox is destroyed after `record`, except while awaiting approval
- [ ] Run `uv run pytest -q`, `-m integration` and `-m sandbox`, all of which must pass. Then commit `feat(agent): solve/approve CLI with Postgres checkpoints`.

### Task 5: Live smoke run with a real model

- [ ] With Ollama running (`REPOPILOT_LLM_BASE_URL=http://127.0.0.1:11434/v1`, `REPOPILOT_LLM_MODEL=qwen2.5:7b-instruct`), run `repopilot solve --task orders-api-005 --auto-approve`. Note the outcome, tokens and time. A failed solve is a finding, not a blocker; M4 measures properly.
