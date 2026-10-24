# RepoPilot — AI Software Engineering Agent: Build Plan

> The plan as written before implementation, kept as design history; notes marked *as built*
> correct statements the build changed. [README.md](../README.md) and
> [architecture.md](architecture.md) describe the system as built.

## Context
The repository starts empty. **RepoPilot** shows model-directed tool orchestration. It takes a GitHub issue, investigates the repo, edits code in a locked-down sandbox, runs tests, recovers from failures within fixed limits, and opens a **draft PR only after a human approves**. Every run is recorded for evaluation.

Success is not beating commercial agents. Success means a reader of the repository can see four things:
- stateful orchestration
- safe tool use
- recovery
- honest evaluation numbers

## Decisions
| Area | Choice |
|---|---|
| Layout | Two services side by side: `services/agent-svc` (Python 3.12, uv, hatchling, ruff line-length 100) and `services/sandbox-svc` (Go 1.25), plus `web/` (Next.js + TS), `bench/`, `db/migrations/`, `eval/`, `docs/` |
| Go's role | **sandbox-svc** is the only process that touches the Docker socket. It exposes a narrow REST API: create workspace, exec an allowlisted command, read/write a file, diff, destroy. The Python agent never gets Docker or host access. This is the security boundary story. |
| Orchestration | LangGraph `StateGraph` + `langgraph-checkpoint-postgres` (`PostgresSaver`). Approval uses `interrupt()`, so a run survives restarts and waits for a human. |
| LLM | One OpenAI-compatible client (`openai` SDK, configurable `base_url`/`model`). It covers hosted APIs and Ollama's `/v1`. `FakeLLM` replays scripted tool calls in tests. |
| Queue | Redis Streams with consumer groups: at-least-once delivery, `XAUTOCLAIM` reclaims stuck jobs. The worker resumes from the LangGraph checkpoint, so redelivery is idempotent. |
| DB | Postgres 16 + pgvector. Raw psycopg 3, hand-written SQL, numbered migrations with a checksum-checked, advisory-locked runner. |
| GitHub | REST via httpx with a fine-grained PAT. The PR is built host-side from the sandbox diff via the Git Data API (blob → tree → commit → ref → draft PR). **The token never enters the sandbox.** |
| Observability | OpenTelemetry spans for each graph node, tool call and LLM call. Langfuse receives them via its OTLP endpoint (optional compose profile; *as built:* the compose profile is `otel`, a local Jaeger, and Langfuse is reached through its OTLP endpoint). The run timeline is also written to Postgres for the UI. |
| Eval | Purpose-built fixture repos with injected bugs, 25 → 50 tasks (*as built:* 25), held-out tests the agent never sees. Metrics are deterministic (no LLM judge), with Wilson CIs. |
| Out of scope | Model training (PyTorch/PEFT), microVM isolation (documented as the next step), multi-tenant auth. AWS stays a docs-and-script milestone at the end. |

## Architecture
```
Next.js UI ──REST/SSE──▶ agent-svc API (FastAPI) ──XADD──▶ Redis Stream
                                  │                              │
                                  ▼                              ▼
                             Postgres ◀── checkpoints/runs ── worker (LangGraph)
                                                                 │ REST
                                                                 ▼
                                             sandbox-svc (Go) ──▶ Docker: per-run container
                                                                  (--network none, non-root,
                                                                   cap-drop ALL, ro rootfs,
                                                                   cpu/mem/pids/time limits)
```

**Graph** (`src/repopilot/graph/`; *as built:* `src/repopilot/agent/`, with the nodes and edges in [architecture.md](architecture.md)):
1. `intake`: fetch the issue, repo metadata and recent commits.
2. `explore`: tool loop using **read-only** tools.
3. `plan`: structured output covering suspected files, root-cause hypothesis, acceptance criteria and test command.
4. `edit`: tool loop with write tools.
5. `validate`: run targeted tests + ruff/lint.
   - On failure: go to `diagnose`, which summarises the failure and returns to `edit`. `retries < MAX_RETRIES (3)`.
   - When the token, cost or wall-clock budget is exhausted: go to `report_failure`.
6. `approval`: `interrupt()` that shows the diff, test output and reasoning. The human approves or rejects with a comment. A rejection with a comment goes back to `edit` once.
7. `open_pr`: create the draft PR.
8. `record`: write the outcome and metrics.

State is a Pydantic model: issue, plan, messages, patches, test results, retry count, budget usage, and policy violations.

**Tools** (`src/repopilot/tools/`). Each is a Pydantic-schema'd function plus a policy check:
- `read_file`
- `list_dir`
- `search_code` (ripgrep in the sandbox; hybrid pgvector search added in M8; *as built:* the hybrid search is a separate, optional `find_code` tool)
- `apply_patch` (unified diff that must apply cleanly)
- `run_tests` (allowlisted commands from the repo config)
- `git_diff`
- `git_log`

`policy.py` does the following:
- denies writes outside the workspace, to `.git/`, to `.github/workflows/`, and to configured protected paths
- caps patch size
- logs every denial as an "unauthorized modification attempt" metric

**Sandbox lifecycle:**
1. A setup container runs the repo's *declared* install command with network access. The agent never chooses this command. (*As built:* there is no setup container; dependencies must be baked into the sandbox image, and nothing is installed at run time.)
2. The agent's container is then started with `--network none`.
3. Fixture repos use prebuilt images with dependencies baked in.

## Directory skeleton
The original layout. *As built*, the agent lives in `agent/` instead of `graph/`, tools in `tools/{registry,policy,builtin}.py`, and the code index in `code_index.py` instead of `retrieval/`.
```
services/agent-svc/src/repopilot/
  config.py  cli.py  api/  worker.py  queue.py  telemetry.py  sandbox_client.py
  graph/{state.py,build.py,nodes/}  tools/{registry.py,policy.py}  llm/{client.py,fake.py}
  github/  db/migrate.py  retrieval/  mcp_server.py  eval/{tasks.py,runner.py,score.py,report.py}
services/sandbox-svc/{cmd/sandbox-svc/main.go, internal/{api,docker,policy}}
bench/repos/<fixture>/  bench/tasks/<id>.yaml  bench/images/
web/  db/migrations/NNN_*.sql  eval/reports/  docs/{architecture.md,plan.md,plans/}
docker-compose.yml  .env.example  .github/workflows/ci.yml
```

## Patterns to reuse
| Area | Pattern |
|---|---|
| Settings | `env_prefix="REPOPILOT_"`, walk-up `.env` lookup, `lru_cache` |
| LLM | Protocol + Fake: one protocol, an OpenAI-compatible client and a scripted fake |
| API | App factory, `{"error":{code,message}}` shape, `X-Trace-Id`, security headers; async |
| Migrations | Numbered SQL files, checksum-checked, applied under an advisory lock |
| Eval | Wilson CI, p50/p95, resumable JSONL reports |
| Retrieval | RRF hybrid search (M8) |
| Tests | `integration` marker, dedicated test DB |
| Go | Layout `cmd/`, `internal/`; slog JSON; distroless multi-stage Dockerfile |
| Docs | README structure: pitch, diagram, rationale, **results with honest limitations** |

## Milestones (each ends green in CI)
- **M0 Scaffold:**
  - `git init`, the layout above, uv project, ruff, pytest markers
  - compose with `pgvector/pgvector:pg16` and `redis:7`, both bound to 127.0.0.1 with healthchecks
  - migrations `001_runs.sql`: runs, run_events, tool_calls, approvals, eval_results
  - CI: ruff + pytest + `go test` + `go vet`
- **M1 Sandbox:**
  - sandbox-svc endpoints and container hardening flags, a timeout/kill path, output truncation
  - Go table-driven tests
  - live sandbox tests (pytest marker `sandbox`) proving: no network, no writes outside the workspace, the fork-bomb pids limit holds, timeouts fire
  - Python `sandbox_client.py`
- **M2 Tools + LLM + first benchmark:**
  - tool registry/policy with unit tests (policy denials included)
  - OpenAI-compatible client with token/cost accounting, plus FakeLLM
  - first fixture repo `orders-api` (small FastAPI app) with 10 tasks
  - task YAML format: issue text, base ref, visible tests, held-out tests, protected paths, expected touched files
- **M3 LangGraph agent:**
  - full graph with Postgres checkpointer, retry/budget edges, `interrupt` approval
  - CLI: `repopilot solve --task <id>` and `repopilot approve <run>`
  - FakeLLM tests covering: happy path, test-failure → diagnose → fix, budget exhausted, policy violation, resume after a simulated crash
- **M4 Eval harness:**
  - `repopilot eval run --split dev|test --model <m>` with auto-approve in eval mode; held-out tests are copied in *after* the agent finishes
  - grow to 3–4 fixture repos (e.g. `orders-api`, `textkit` lib, `cronparse`, `inventory-cli`) and 25 tasks, then 50, split simple/complex, test split frozen with sha256 (*as built:* three repos, `orders-api`, `textkit` and `inventory`, with 25 tasks)
  - metrics:
    - resolution rate on held-out tests
    - patch-apply rate and visible-test pass rate
    - tool-call failure rate
    - average retries
    - p50/p95 time
    - tokens and $ per resolved task
    - unauthorized-attempt count
  - reports go to `eval/reports/*.{jsonl,md,meta.json}`
- **M5 Service mode:**
  - async FastAPI (*as built:* under `/v1`, as `/v1/runs…` and `/v1/evals`):
    - `POST /runs` (repo + issue number or text)
    - `GET /runs/{id}`
    - `GET /runs/{id}/events` (SSE)
    - `POST /runs/{id}/approval`
    - `GET /eval/reports`
  - Redis Streams worker with reclaim
  - GitHub client: read issue/files/commits; draft PR via the Git Data API after approval
- **M6 Observability:**
  - OTel tracer setup in `telemetry.py`
  - spans for nodes/tools/LLM with token attributes
  - Langfuse via OTLP (compose profile `langfuse`; *as built:* the profile is `otel`, a local Jaeger, and Langfuse takes the same OTLP export)
  - trace id stored on each run and linked from the UI
- **M7 Next.js UI:**
  - runs list
  - run detail: plan, live tool-call timeline over SSE, diff viewer, test output, Approve/Reject with comment
  - eval dashboard: metrics table and per-task results
- **M8 Extras:**
  - pgvector + full-text hybrid code index used by `search_code`, for semantic queries over chunked files and commit messages (*as built:* behind its own optional tool, `find_code`)
  - MCP server (`mcp_server.py`, stdio) exposing the same tools
  - **Ollama baseline eval** compared against the hosted model (*as built:* the Ollama runs exist; the hosted-model run has not been made)
- **M9 Ship:**
  - README with the results table and limitations (including that Docker is not a full security boundary; microVMs/gVisor are next)
  - `docs/architecture.md`
  - AWS deploy notes/script (single EC2 + compose)
  - demo GIF

Each milestone gets its own detailed plan in `docs/plans/YYYY-MM-DD-mN-*.md` before it is built (*as built:* M0–M5 have one; M6–M9 have no separate plan). Commits use conventional prefixes.

## Verification
- **Unit:** run `uv run pytest` (FakeLLM, no network) and `go test ./...`. Both run in CI on every push.
- **Integration:** run `uv run pytest -m integration` against compose Postgres/Redis, and `uv run pytest -m sandbox` against the real sandbox-svc. Both are selected by pytest marker; a plain `uv run pytest` deselects them. Sandbox escape tests must fail closed.
- **End-to-end (local):**
  1. Run `docker compose up -d`, then `repopilot migrate`.
  2. Run `repopilot solve --task orders-api-001`.
  3. Inspect the diff and approve.
  4. Check that a draft PR appears on a throwaway GitHub repo.
  5. Check that the run, tool calls and trace are in Postgres and Langfuse.
- **Eval:** run `repopilot eval run --split test` to produce the metrics report. Its headline numbers go into the README unedited.
- **UI:** drive the run-detail and approval flow in a browser.

## Status (2026-10-21)

| Milestone | State |
|---|---|
| M0 Scaffold | ✅ merged; reviewed |
| M1 Sandbox | ✅ merged; reviewed (3 Important fixed: tar breakout, symlinked-dir reads, escaped fork bombs) |
| M2 Tools, LLM, first benchmark | ✅ merged; reviewed (1 Critical fixed: patch policy bypass) |
| M3 LangGraph agent | ✅ merged; reviewed (1 Critical fixed: deletions hidden from validation) |
| M4 Eval harness + 25 tasks | ✅ reviewed (5 Important + minors fixed: leaked fixes, objective difficulty, infra errors retried, final-workspace patch metric, resume config guard); dev v1 4/15, dev v2 5/15, frozen test 1/10 (7B) and 0/10 (3B baseline) |
| M5 Service (API, queue, worker, GitHub PRs) | ✅ reviewed (1 Critical + 7 Important + minors fixed); live smoke: console run → review → approve → hidden tests graded |
| M6 Observability | ✅ spans per run/node/LLM/tool; trace id on runs and in the console |
| M7 Web console | ✅ type-checked and built in CI; used for the live smoke and demo |
| M8 MCP server, hybrid code index | ✅ tests pass; MCP opens only allowed roots. The planned hosted-model comparison has not been run |
| M9 Ship (README, architecture, AWS, demo) | ✅ results, demo GIF, architecture and AWS notes |

Review outcomes are summarised in this table; the commit history has the detail.
