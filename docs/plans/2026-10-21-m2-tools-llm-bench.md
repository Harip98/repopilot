# M2 Tools, LLM Layer and First Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build everything the agent loop in M3 consumes:
- an OpenAI-compatible chat client with token and cost accounting, plus a scripted fake
- a policy-checked tool registry that runs against a sandbox
- the benchmark task format
- the first fixture repo, `orders-api`, with 10 validated tasks

Validated means that on the buggy base the held-out tests fail, and with the reference fix they pass.

**Architecture:**
- **LLM layer.** `repopilot.llm` defines a `ChatModel` protocol (`async chat(messages, tools) -> ChatResult`). It is implemented by `OpenAICompatChat`, built on the `openai` SDK. The same class serves hosted APIs and Ollama's `/v1`.
- **Tools.**
  - `repopilot.tools` turns Pydantic argument models into JSON-schema tool specs.
  - Dispatch validates the arguments, applies `ToolPolicy` to every write, runs the tool through `SandboxClient`, and returns a truncated, model-readable `ToolOutcome`. Tools never raise to the caller.
- **Benchmark.**
  - `bench/repos/<repo>/` holds a clean fixture repo. `bench/tasks/<id>/` holds `task.yaml`, `bug.patch`, `fix.patch` and `heldout/`.
  - `repopilot.bench` builds a task's base workspace on the host:
    1. copy the repo
    2. `git init` and commit "initial"
    3. apply `bug.patch` and commit it with the task's plausible commit message, so the agent can find the culprit in `git log`
  - Fixture images (`repopilot-bench-<repo>`) extend the base image with the repo's dependencies baked in.

**Tech Stack:** `openai>=1.50` (AsyncOpenAI), Pydantic v2, PyYAML, httpx, host `git`, anyio pytest plugin.

**Spec:** [docs/plan.md](../plan.md), sections Decisions ("LLM", "Eval"), Tools, and M2.

## Global Constraints

- **Tool names and arguments.** These are exact. All paths are workspace-relative.
  - `read_file(path, start_line=1, end_line=None)`: returns numbered lines. At most 400 lines per call; say so when truncated.
  - `list_dir(path=".", depth=2)`: depth 1..4. `.git` is excluded.
  - `search_code(query, path=".", regex=False)`: runs `rg -n --no-heading --color never`, at most 50 matches. `-F` is used unless `regex`.
  - `replace_text(path, old, new)`: `old` must occur exactly once. On 0 or more than 1 matches, the tool says how many.
  - `create_file(path, content)`: fails if the file exists.
  - `apply_patch(patch)`: a unified diff. Target paths are parsed from `---`/`+++` headers and checked against policy. The patch is written to `.git/rp-patch.diff`, checked with `git apply --check`, then applied with `git apply --whitespace=nowarn`.
  - `run_tests(target=None)`: runs the task's `test_command` argv. `target` is an optional pytest node id whose file part must pass `CleanRelPath`-equivalent checks. Timeout 300 s.
  - `run_lint()`: `ruff check --output-format concise .`
  - `git_diff()`: runs `git add -A --intent-to-add` and then `git diff`.
  - `git_log(path=None, n=10)`: n is 1..30.
- **Tool groups.**
  - Read-only: `read_file`, `list_dir`, `search_code`, `git_diff`, `git_log`.
  - Write: `replace_text`, `create_file`, `apply_patch`.
  - Exec: `run_tests`, `run_lint`.
  - The registry can be built with any subset. M3's explore phase gets read-only tools only.
- **ToolOutcome.**
  - Fields: `ok: bool`, `content: str`, `denied: bool`, `error: str | None`, `latency_ms: int`.
  - `content` is capped at 8000 characters, with the marker `\n…[truncated N chars]` when cut.
  - Invalid JSON or arguments give `ok=False` and an `error` naming the problem. Nothing is raised.
- **ToolPolicy.**
  - Writes are denied to `.git/**`, `.github/**`, any path in the task's `protected_paths` globs (`fnmatch`), and paths failing workspace-relative validation.
  - Patches over 64 KiB are denied.
  - A denial gives `ok=False`, `denied=True` and `error="policy: <reason>"`.
- **ChatResult.** Fields: `message: AssistantMessage` (`content: str | None`, `tool_calls: list[ToolCall(id, name, arguments: str)]`) and `usage: Usage(prompt_tokens, completion_tokens)`.
- **Cost.** `cost_usd = (prompt × llm_price_in + completion × llm_price_out) / 1e6`. The prices are new `Settings` fields in USD per 1M tokens, defaulting to `0.0` (local models).
- **Determinism.** The client is called with `temperature=0`. `FakeChatModel` replays a scripted list and fails loudly when it runs out.
- **Task YAML.** Required keys:
  - `id` (`^[a-z0-9-]+-\d{3}$`), `repo`, `title`, `issue`, `commit_message`
  - `difficulty: simple|complex`, `split: dev|test`
  - `test_command` (argv), `heldout_command` (argv)
  - `protected_paths` (globs), `expected_files` (paths)
  - `heldout/` mirrors repo paths, for example `heldout/tests/test_heldout_pagination.py`.
- **Fixture repos** are tiny, real-looking Python projects with a `pyproject.toml` (`pythonpath = ["."]`) and visible tests that pass on the clean repo. Held-out tests are never in the agent's workspace during a run.

## Review Focus

1. **The model sends tool arguments that are not JSON, or are JSON with wrong types.** It gets an error message back and the loop continues. Test: `test_dispatch_bad_arguments` (Task 2).
2. **A patch whose `+++` header targets `.github/workflows/ci.yml`, or a protected path.** It is denied, nothing is written, and `denied=True` is recorded. Test: `test_apply_patch_denied_paths` (Task 3).
3. **`replace_text` where `old` appears twice.** No change, and the count is reported. Test: `test_replace_text_ambiguous` (Task 3, sandbox).
4. **A task whose bug patch no longer applies, or whose held-out tests pass on the buggy base.** Validation fails loudly. Test: `test_validate_tasks` (Task 5, sandbox).
5. **An OpenAI-compatible server that returns no `usage` (some Ollama versions).** Tokens are recorded as 0 and nothing crashes. Test: `test_openai_compat_missing_usage` (Task 1).

---

### Task 1: LLM layer

**Files:**
- Create: `src/repopilot/llm/__init__.py`, `llm/types.py`, `llm/client.py`, `llm/fake.py`
- Modify: `config.py` (add `llm_price_in`, `llm_price_out`, `llm_timeout_s=120`), `pyproject.toml` (add `openai>=1.50`)
- Test: `tests/llm/test_client.py`

**Interfaces:**
- Produces:
  - `Usage`, `ToolCall`, `AssistantMessage`, `ChatResult`, `ToolSpec(name, description, parameters: dict)`. Messages are plain dicts in OpenAI chat format.
  - `class ChatModel(Protocol): model: str; async def chat(self, messages: list[dict], tools: list[ToolSpec] | None = None, max_tokens: int = 2048) -> ChatResult`
  - `OpenAICompatChat(base_url, api_key, model, timeout_s, *, http_client: httpx.AsyncClient | None = None)`
  - `FakeChatModel(script: list[ChatResult], model="fake")`, which exposes `.calls: list[dict]`
  - `cost_usd(usage: Usage, price_in: float, price_out: float) -> float`
  - `get_chat_model() -> OpenAICompatChat` (from settings)

Steps:
- [ ] Write failing tests:
  - `test_openai_compat_parses_tool_calls`: an `httpx.MockTransport` serves `/v1/chat/completions` with one tool call and usage, which parse into a `ChatResult`. The request body has `temperature: 0`, the `tools` array in function format, and `model`.
  - `test_openai_compat_missing_usage`: no usage gives `Usage(0, 0)`.
  - `test_fake_replays_and_exhausts`: the fake returns its script in order and records the calls; one call too many raises `AssertionError`.
  - `test_cost_usd`: 1000 in and 500 out at $0.4/$1.6 gives `0.0012`.
- [ ] Run `uv run pytest tests/llm -q`. Expected: FAIL. Implement. Run again. Expected: PASS. Run `ruff check .` clean.
- [ ] Commit `feat(llm): OpenAI-compatible chat client, fake model and cost accounting`.

### Task 2: Tool registry and policy (no sandbox)

**Files:**
- Create: `src/repopilot/tools/__init__.py`, `tools/policy.py`, `tools/registry.py`, `tools/patch.py` (diff header parsing)
- Test: `tests/tools/test_policy.py`, `tests/tools/test_registry.py`

**Interfaces:**
- Consumes: `ToolSpec` from Task 1; `SandboxClient` and `SandboxError` from M1.
- Produces:
  - `ToolPolicy(protected_paths: list[str], max_patch_bytes=65536)` with `.check_write(path) -> str | None`, which returns the denial reason
  - `patch_targets(diff: str) -> list[str]`, which handles `a/`/`b/` prefixes, `/dev/null`, and `rename from/to`
  - `ToolContext(sandbox: SandboxClient, sandbox_id: str, policy: ToolPolicy, test_command: list[str])`
  - `ToolOutcome`
  - `Tool(name, description, args_model: type[BaseModel], fn: async (ctx, args) -> str, kind: Literal["read","write","exec"], write_paths: Callable[[args], list[str]] | None)`
  - `ToolRegistry(tools)` with `.specs() -> list[ToolSpec]`, `.subset(kinds) -> ToolRegistry`, and `async .dispatch(ctx, name, arguments: str) -> ToolOutcome`
  - `truncate(text, limit=8000) -> str`

Steps:
- [ ] Write failing tests:
  - `test_policy_denies`: denied for `.git/config`, `.github/workflows/ci.yml`, `tests/heldout_x.py` with protected `tests/heldout_*`, `../x`, `/etc/x`; allowed for `app/orders.py`.
  - `test_patch_targets`: parses modify, new-file and delete diffs plus a rename.
  - `test_dispatch_bad_arguments`: non-JSON, a missing field and a wrong type each give `ok=False` with a readable error; the tool fn is never called.
  - `test_dispatch_unknown_tool`.
  - `test_dispatch_policy_denied`: a write tool targeting `.git/x` gives `denied=True` and the fn is not called.
  - `test_dispatch_sandbox_error`: a fn raising `SandboxError` gives `ok=False` with code and message.
  - `test_truncate`.
  - `test_specs_json_schema`: a `read_file` spec has `path` required and `start_line` optional.
- [ ] Run, watch it FAIL, implement, watch it PASS, then commit `feat(tools): policy-checked tool registry with safe dispatch`.

### Task 3: The ten tools against a sandbox

**Files:**
- Create: `src/repopilot/tools/builtin.py` (`build_registry() -> ToolRegistry` with all ten tools)
- Test: `tests/tools/test_builtin_unit.py` (fake sandbox), `tests/sandbox/test_tools_live.py` (marker `sandbox`)

**Interfaces:**
- Consumes: the Task 2 registry and the M1 client.
- Produces: `build_registry()`, plus the exact tool names and argument schemas from Global Constraints.

Steps:
- [ ] Write failing unit tests with an in-memory fake `SandboxClient` (`read_file`/`write_file`/`exec` recorded):
  - `test_read_file_numbers_and_caps`
  - `test_apply_patch_denied_paths`
  - `test_run_tests_rejects_bad_target`: `../x` and `-k` injection such as `--rootdir=/` are rejected
- [ ] Write live tests against `repopilot-sandbox-base` with a small committed repo:
  - `test_read_list_search`
  - `test_replace_text_ok_and_ambiguous`
  - `test_create_file_exists`
  - `test_apply_patch_applies_and_rejects_bad_patch`
  - `test_run_tests_pass_and_fail_output`
  - `test_git_diff_includes_new_files`
  - `test_git_log`
- [ ] Run `uv run pytest tests/tools -q` and `uv run pytest -m sandbox -q`. Expected: FAIL, then implement, then PASS.
- [ ] Commit `feat(tools): read/search/edit/patch/test/git tools over the sandbox`.

### Task 4: Benchmark format, workspace builder and fixture image

**Files:**
- Create: `src/repopilot/bench/__init__.py`, `bench/tasks.py`, `bench/workspace.py`
- Create: `bench/repos/orders-api/...` (the clean repo), `bench/images/orders-api/Dockerfile` (FROM base, installs `fastapi` and `httpx`)
- Modify: `cli.py` (`repopilot bench list`, `repopilot bench build-images`)
- Test: `tests/bench/test_tasks.py`, `tests/bench/test_workspace.py`

**Interfaces:**
- Produces:
  - `Task`: a Pydantic model of the YAML keys plus `dir: Path`
  - `load_tasks(split: str | None = None, root=REPO_ROOT / "bench" / "tasks") -> list[Task]`, which validates ids, file existence and `repo` existence
  - `build_base_workspace(task, dest: Path) -> Path`: copy the repo, `git init -q -b main`, commit "initial" with a fixed author/date env, `git apply bug.patch`, commit `task.commit_message`
  - `image_for(task) -> str`, which returns `repopilot-bench-<repo>`

**orders-api** contents:
- `app/main.py`: FastAPI app with routes `GET /orders?page=&page_size=&status=&sort=`, `GET /orders/{id}`, `POST /orders`, `POST /orders/{id}/cancel`
- `app/store.py`: in-memory seed of 57 orders
- `app/pagination.py`
- `app/pricing.py`: subtotal, discount and tax with `Decimal`
- `app/models.py`: Pydantic
- `tests/`: visible tests

Steps:
- [ ] Write failing tests:
  - `test_load_tasks_validates`: a bad id, a missing `bug.patch` and an unknown repo each raise `ValueError` naming the problem
  - `test_build_base_workspace_history`: the result has 2 commits, the last subject equals `commit_message`, the working tree is clean, and no `heldout/` file is present
- [ ] Write the clean `orders-api` and its Dockerfile. Its visible tests pass when run locally with `uv run --with fastapi --with httpx --with pytest pytest bench/repos/orders-api`. Add `bench build-images`, which runs `docker build` for `bench/images/*` in dependency order, base first.
- [ ] Implement, PASS, then commit `feat(bench): task format, workspace builder and orders-api fixture`.

### Task 5: Ten orders-api tasks and validation

**Files:**
- Create: `bench/tasks/orders-api-001` … `orders-api-010`, each with `task.yaml`, `bug.patch`, `fix.patch` and `heldout/`
- Create: `src/repopilot/bench/validate.py`; `cli.py` gains `repopilot bench validate`
- Test: `tests/sandbox/test_bench_validate.py` (marker `sandbox`)

**Tasks:** 6 simple and 4 complex. 7 are `dev` and 3 are `test`; the test split is ids 008–010.

| id | Bug | Kind |
|---|---|---|
| 001 | pagination skips the last partial page (off-by-one in page count) | simple |
| 002 | `page_size` cap of 100 not enforced | simple |
| 003 | `sort=-created_at` sorts ascending | simple |
| 004 | discount applied after tax instead of before | simple |
| 005 | status filter is case-sensitive | simple |
| 006 | cancelling a shipped order is allowed | simple |
| 007 | money rounding uses float and drifts by a cent on some totals | complex |
| 008 | page beyond the end returns 500 instead of an empty page with correct `total` | complex |
| 009 | cancelled line items still counted in subtotal | complex |
| 010 | `POST /orders` with duplicate `Idempotency-Key` creates a second order | complex |

**Interfaces:**
- Produces: `validate_task(task, sandbox) -> ValidationResult(task_id, bug_applies, visible_pass_on_base, heldout_fail_on_base, heldout_pass_with_fix, visible_pass_with_fix)`. A task is valid when all checks are true. The exception is `visible_pass_on_base`, which is recorded but not required: a bug may break visible tests, and the agent sees that.

Steps:
- [ ] Write `test_validate_tasks`: for every task, `validate_task` holds.
- [ ] Write the ten tasks. For each one:
  - the issue text reads like a real GitHub issue, describing symptoms rather than the fix
  - the held-out tests pin the behaviour
  - `fix.patch` is the minimal reference fix
- [ ] Run `uv run repopilot bench build-images && uv run pytest -m sandbox -q -k validate`. Expected: 10/10 valid.
- [ ] Commit `feat(bench): ten validated orders-api tasks`.
