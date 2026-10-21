# RepoPilot

An AI software engineering agent that takes an issue, investigates the repository, fixes the
code inside a locked-down sandbox, proves the fix with tests, and opens a **draft pull request
only after a person approves it**. Every step is checkpointed, recorded and measurable.

<!-- Demo: docs/demo.gif (recorded run: issue → plan → diff → tests → approval) -->

RepoPilot is about **model-directed tool orchestration done safely**. Its companion project,
ind-agent, is about evidence-grounded, deterministic AI.
The goal here is not to beat commercial coding agents. It is to show:

- stateful orchestration;
- tools the model cannot misuse;
- recovery from failure;
- evaluation honest enough to argue with.

## What it does

| Step | How |
|---|---|
| Read the issue and the repo | Read-only tools first: `read_file`, `search_code` (ripgrep), `list_dir`, `git_log`, optional hybrid `find_code` |
| Plan | The model must call `submit_plan` (root cause, files, acceptance criteria) before it gets any editing tools |
| Edit in a sandbox | `replace_text`, `create_file`, `apply_patch`, run in a per-run container with no network |
| Validate | The harness, not the model, checks the full diff against protected paths and runs the tests, before and after |
| Recover | Test failures go back to the model, within limits on retries, model calls, tool calls, tokens and time |
| Ask for approval | Diff, test output and reasoning are shown; the run waits, durably, for approve, reject, or reject with a comment (one revision) |
| Open a PR | Built on GitHub through the Git Data API as a **draft**; the GitHub token never enters the sandbox |
| Record | Every model call, tool call (with policy denials), status change and trace goes to Postgres and OpenTelemetry |

## How it fits together

```
 Next.js console ──► FastAPI ──► Redis Stream ──► worker: LangGraph agent ──► sandbox-svc (Go) ──► container
       ▲  SSE           │                             │      │                    only process with      no network,
       └────────────────┴──────── Postgres ◄──────────┘      └─► LLM (any OpenAI-  the Docker socket   read-only root,
                         runs · events · tool calls · evals ·     compatible API,                       uid 10001
                         LangGraph checkpoints · code index        incl. Ollama)
```

```
intake ─► llm ⇄ tools ─finish─► validate ─pass─► approval ─approve─► open_pr ─► record
           │                       └─fail─► diagnose ─► llm (bounded retries)
           └─budget─► fail ─► record                  reject + comment ─► llm (once)
```

The full design is in [docs/architecture.md](docs/architecture.md).

## Design decisions

- **The harness validates, not the model.** `finish` only asks for validation. The harness
  then diffs against `HEAD`, which covers deletions, both sides of renames and files hidden by
  `.gitignore`. It rejects changes to protected paths, runs the tests, and checks the workspace
  again afterwards.
- **The write policy trusts git, not a regex.** `apply_patch` asks `git apply --numstat` which
  paths a patch touches, which closes five bypasses through quoted names, prefixes, header-only,
  binary and renamed diffs. A second, independent check then runs at validation.
- **Durable by construction.** Every graph step is a Postgres checkpoint. A worker that dies is
  replaced by another that continues from the last step, and a worker renews its job's lease
  while it runs. Approval is an `interrupt()`; two concurrent approvals cannot both resume a
  run. The run's sandbox is reaped after 2 hours, so an approval later than that fails the
  run as `sandbox_expired`.
- **One process holds the Docker socket.** `sandbox-svc` is small, written in Go with the
  standard library only, authenticated and loopback-only. The Python agent never touches
  Docker.
- **The benchmark measures what users care about.** Held-out tests the agent never sees, issues
  written as symptoms, no fix hidden in `.git`, scoring immune to `conftest.py` tricks, and a
  frozen test split.

## Results

> **Status:** dev split measured, and the agent tuned from it. The frozen test split is run once
> and reported here unedited.

Benchmark: **25 tasks** across three fixture repos.

| Repo | Tasks | What it is |
|---|---|---|
| `orders-api` | 10 | FastAPI orders service: pagination, money and status rules |
| `textkit` | 8 | Text utilities: slugs, truncation, CSV, identifier case |
| `inventory` | 7 | FIFO cost ledger, reorder points, CSV import |

The split is 15 dev and 10 test, and the test split is frozen by per-task hashes. Every task is
validated: its hidden tests fail on the bug and pass with the reference fix.

| Run | Model | Resolved | 95% CI | Tool-call failures | Median time | Notes |
|---|---|---|---|---|---|---|
| dev v1 | qwen2.5 7B (local, Ollama) | 4 / 15 (26.7%) | 11–52% | 3.3% of 122 | 185 s | Before tuning: 7 runs ended on unparseable tool calls |

Each eval writes its full report, with per-task outcomes, failure reasons, token use and policy
denials, to [`eval/reports/`](eval/reports/).

**Read these numbers with their limits:**

- The model is a 7B model on a 4 GB laptop GPU. A hosted model is a one-variable switch
  (`REPOPILOT_LLM_BASE_URL`, `REPOPILOT_LLM_MODEL`); the harness is the same.
- Every task's *visible* tests pass on the buggy code, so the agent has to reproduce the bug
  from the issue. A green suite is weak evidence, and the UI and PR body say so.
- The fixtures are small, and fixes touch one or two files.
- Ollama reports prompt tokens without its cached prefix, so local token counts are low.

## Stack

| Layer | Technology |
|---|---|
| Agent | Python 3.12, LangGraph (Postgres checkpointer), Pydantic, OpenAI-compatible chat API, Ollama |
| Tools | Structured tool calling with JSON-schema arguments; also served over **MCP** (`repopilot mcp`) |
| Sandbox | Go 1.25 service on the Docker Engine API; hardened containers |
| Service | FastAPI, SSE, Redis Streams (consumer groups, reclaim), psycopg 3 |
| Data | PostgreSQL 16 + pgvector (runs, events, evals, checkpoints, hybrid code index with RRF) |
| Observability | OpenTelemetry (GenAI semantic conventions) → Jaeger or Langfuse |
| Evaluation | pytest, a 25-task benchmark with held-out tests, Wilson intervals, frozen test split |
| UI | Next.js 15 + TypeScript |
| Delivery | Docker Compose, GitHub Actions, AWS notes and user-data ([docs/deploy-aws.md](docs/deploy-aws.md)) |

## Run it

Prerequisites: Docker, [uv](https://docs.astral.sh/uv/), Node 22, and either an
OpenAI-compatible API key or [Ollama](https://ollama.com).

```bash
cp .env.example .env                       # set the REPOPILOT_LLM_* values
docker compose up -d                       # Postgres, Redis, sandbox-svc
cd services/agent-svc
uv sync
uv run repopilot migrate
uv run repopilot bench build-images        # sandbox images for the fixture repos
uv run repopilot bench validate            # proves all 25 tasks are sound
```

Solve one task from the terminal, then review it:

```bash
uv run repopilot solve --task orders-api-005
uv run repopilot approve <run-id>          # or: --reject --comment "use casefold"
```

Or run the service and the console:

```bash
uv run repopilot serve                     # API on 127.0.0.1:8000
uv run repopilot worker                    # executes queued runs
cd ../../web && npm ci && npm run dev      # console on 127.0.0.1:3000
```

Evaluate:

```bash
uv run repopilot eval run --name my-dev --split dev
uv run repopilot bench freeze --note "before final test run"
uv run repopilot eval run --name my-test --split test
uv run repopilot eval compare my-dev my-test
```

GitHub issues: set `REPOPILOT_GITHUB_TOKEN` (a fine-grained token with issues read, and
contents and pull requests write), then start a run with `{"repo": "owner/name",
"issue_number": 42}` from the console or the API. Add a `.repopilot.yml` to the target repo
for its test command and sandbox image.

## Sandbox security model

Agent edits and test runs happen in a throwaway container per run, created and driven only by
`sandbox-svc` (Go). The agent service never touches Docker; it calls sandbox-svc's REST API with
a bearer token.

| Control | Setting |
|---|---|
| Network | `--network none`: no egress, no ingress |
| User | uid/gid 10001, never root; `no-new-privileges`; all capabilities dropped |
| Filesystem | Read-only root; `/workspace` is a 512 MB tmpfs mounted `nodev,nosuid`; `/tmp` is a 256 MB tmpfs |
| Resources | 1 CPU, 1 GiB memory with no swap, 256 PIDs |
| Mounts | The workspace volume only: no host paths, no Docker socket, no secrets |
| Time | Every command runs under `timeout` (default 120 s, max 600 s), with a stream-deadline backstop |
| Leftovers | Processes a command leaves behind are killed; if none can start, the sandbox restarts and the workspace is restored |
| Output | stdout and stderr capped at 64 KiB each |
| Images | Only images named `repopilot-*` |
| Lifetime | Sandboxes older than 2 h are reaped, including orphans from a crashed service |

Files never cross the boundary through a shell:

- **Uploads** are rewritten by sandbox-svc before Docker sees them:
  - only regular files, directories and symlinks are kept;
  - names must stay inside `/workspace`, and nothing may be written beneath a symlink;
  - setuid and setgid bits are cleared;
  - parent directories are emitted explicitly, so a symlink planted earlier is replaced, not
    followed.
- **Reads** run as the sandbox user and open every path component with `O_NOFOLLOW`.

What this is **not**:

- **The command allowlist is defence in depth, not the boundary.** `python` can do anything a
  shell can. The container is the boundary.
- **sandbox-svc is root-equivalent on the host**, because it holds the Docker socket. That is
  why it is small, dependency-free, loopback-only, authenticated and locked down.
- **Containers share the host kernel.** For untrusted public use, the next step is gVisor or
  Firecracker. See [docs/deploy-aws.md](docs/deploy-aws.md).

## Tests

```bash
cd services/agent-svc
uv run pytest                    # unit: graph, tools, policy, scoring, API models (fakes, no network)
uv run pytest -m integration     # Postgres and Redis: recorder, migrations, API, queue
uv run pytest -m sandbox         # live sandbox: isolation, tools, task validation, end-to-end solve
cd ../sandbox-svc && go test ./...
```

## Project layout

```
services/agent-svc   Python: agent graph, tools, bench, eval, API, worker, MCP server
services/sandbox-svc Go: hardened sandbox service
web/                 Next.js review console
bench/               fixture repos, task generators, 25 tasks, sandbox images
db/migrations/       numbered SQL migrations
eval/reports/        eval results (JSONL, Markdown, metadata)
docs/                architecture, plans per milestone, AWS deployment
```

## License

MIT
