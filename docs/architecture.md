# RepoPilot architecture

RepoPilot is a tool-using agent that takes a software issue, investigates the repository,
changes code inside a locked-down sandbox, proves the change with tests, and opens a draft pull
request only after a person approves it. This document explains how the pieces fit and why they
are built the way they are.

## 1. Components

```
            ┌──────────────── web (Next.js) ────────────────┐
            │  runs · run review (track, diff, tests) · evals │
            └───────────────┬────────────────────────────────┘
                            │ /api/rp/* (server-side proxy adds the bearer token)
                            ▼
 repopilot serve ──► FastAPI ── Postgres (runs, events, tool calls, evals, checkpoints)
                        │  ▲
                 XADD   │  │ SSE (polls run_events)
                        ▼  │
                 Redis Stream ──XREADGROUP──► repopilot worker (LangGraph agent)
                                                 │         │            │
                                       OpenAI-compatible   │ REST       │ REST (host only)
                                       LLM (hosted/Ollama) ▼            ▼
                                              sandbox-svc (Go)       GitHub API
                                                 │ Docker socket
                                                 ▼
                                   one hardened container per run
```

| Component | Language | Job |
|---|---|---|
| `services/agent-svc` | Python 3.12 | Agent graph, tools, evaluation, API, worker, CLI, MCP server |
| `services/sandbox-svc` | Go 1.27, stdlib only | The only process with the Docker socket; creates and drives sandboxes |
| `web` | Next.js 15, TypeScript | Review console |
| Postgres 16 + pgvector | | Runs and their timelines, eval results, LangGraph checkpoints, optional code index |
| Redis 7 | | Durable job queue (Streams with a consumer group) |

## 2. A run, step by step

The agent is a LangGraph state machine. One node execution is one checkpointed step, so a run
survives a crashed worker and resumes mid-investigation.

```
intake ─► llm ⇄ tools ─finish─► validate ─pass─► request_review ─► approval ─approve─► open_pr ─► record
          │                     │                                  ├─reject + comment (once)─► llm
          │                     │                                  └─reject─► record
          │                     ├─fail, retries left (max_retries)─► diagnose ─► llm
          │                     └─protected path changed · retries used up · sandbox error─► fail
          ├─reply without a tool call (up to 3 nudges in a row)─► nudge ─► llm
          └─budget exceeded · llm_error · no_tool_calls─► fail ─► record
```

These are the edges of `EDGES` in `repopilot/agent/graph.py`. `llm ⇄ tools` loops until the
model calls `finish`. A reply without a tool call gets a nudge; after three nudges in a row,
the next such reply fails the run with `no_tool_calls`. A tool call resets the count.

1. **intake** writes the system prompt and the issue, plus a two-level file listing so the model
   starts from the real layout.
2. **explore**: the model gets read-only tools (`read_file`, `list_dir`, `search_code`,
   `git_log`, `git_diff`, optionally `find_code`) and must call `submit_plan` with a root cause,
   the files it will change, acceptance criteria and test targets.
3. **edit**: all tools, plus `finish`.
4. **validate** is run by the harness and never trusts the model. It computes the full change
   against `HEAD` (edits, new files, deletions, both sides of renames, and files hidden by
   `.gitignore`), rejects the run if any protected path changed, runs the test command, and
   re-checks the workspace after the tests, because test code can touch files too.
5. **diagnose** hands the failing output back to the model, at most `max_retries` times.
6. **request_review** stores the review payload (diff, test output, plan, summary, and whether
   the suite already passed before the change) on the run row, and **approval** pauses with
   `interrupt()`. They are separate nodes because LangGraph re-runs the interrupted node on
   resume: a status write inside it would reopen the review while the PR is being opened. A
   rejection with a comment gets one revision.
7. **open_pr** builds a draft PR for GitHub runs; benchmark runs have nowhere to push.
8. **record** writes the final status, usage and result.

**Budgets** cover model calls, tool calls, tokens and wall-clock seconds. They are checked before
every model call and before every tool call inside a turn, so one reply asking for 60 tool
calls cannot exceed the limit. Time spent waiting for the reviewer is not counted.

## 3. Sandbox

Every run gets one container from an allowlisted `repopilot-sandbox-*` or `repopilot-bench-*`
image, created by sandbox-svc:

- `--network none`; uid/gid 10001; all capabilities dropped; `no-new-privileges`;
- read-only root filesystem; `/workspace` is a 512 MB tmpfs mounted `nodev,nosuid`;
- 1 CPU, 1 GiB memory without swap, 256 PIDs;
- every command under `timeout` (default 120 s, at most 600 s), output capped at 64 KiB per
  stream, and a stream-deadline backstop;
- after every command, processes it left behind are killed; if not even that can start
  (a fork bomb that escaped `timeout`), the container is restarted and the workspace restored
  from a daemon-side snapshot;
- sandboxes older than 2 h are reaped, including orphans from a crashed service.

Files never cross the boundary through a shell. Uploads are rewritten before Docker sees them
(regular files, directories and symlinks only; no paths outside `/workspace`; nothing written
beneath a symlink; setuid bits cleared; parents emitted explicitly). Reads run inside the
sandbox as the sandbox user and open every path component with `O_NOFOLLOW`.

sandbox-svc holds the Docker socket, which is root-equivalent on the host. That is why it is
small, has no third-party dependencies, listens on loopback, requires a token, and runs
read-only with every capability dropped. Containers still share the host kernel; for untrusted
public use the next step is gVisor or Firecracker at the container-create call.

## 4. Tools and the write policy

Ten tools, each a Pydantic-validated function that runs through sandbox-svc:

| Read | Write | Execute |
|---|---|---|
| `read_file`, `list_dir`, `search_code`, `git_log`, `git_diff` (+ optional `find_code`) | `replace_text`, `create_file`, `apply_patch` | `run_tests`, `run_lint` |

`find_code` is the optional hybrid code index (pgvector + full-text, fused with RRF). It is
enabled with `--code-index` on `solve` and `eval run`, or with `REPOPILOT_CODE_INDEX=true` for
runs the worker executes. The index is built from the imported tree before the sandbox starts
and dropped when the run is over.

Dispatch never raises. Bad JSON, bad arguments, policy denials and sandbox errors all come back
as a tool result the model can read and act on. Each call is checked against exactly the tools
the model was offered for that reply. A call that was not offered, names an unknown tool or
would exceed the budget is refused. It is still recorded (with `refused = true`), but it is not
counted in the run's usage totals.

`ToolPolicy` denies writes to `.git/` (at any depth), `.github/`, and each task's protected
globs, which for the benchmark include held-out test names, `conftest.py` and pytest
configuration. For `apply_patch`, the policy checks **the paths git itself reports**
(`git apply --numstat -z`), because a naive header parser can be fooled by quoted names,
non-`a/` prefixes, header-only, binary and renamed diffs. Every denial is recorded as an
unauthorized modification attempt.

## 5. Persistence

- `runs`: one row per run, with status, outcome, usage and the review payload. The usage
  counters are updated as the run progresses, not only when it ends. A GitHub run's `issue_ref`
  is `owner/name#N`.
- `run_events`: an ordered timeline. Sequence numbers are allocated under a row lock, so SSE
  clients can resume with `Last-Event-ID`.
- `tool_calls`: every tool call, with its arguments, outcome, denial flag and latency. Calls that
  were requested but not run are kept with `refused = true`, so the console can pair each model
  reply with its calls.
- `approvals`: one row per human decision and review round (approve, reject, or reject with a
  comment), with the reviewer and the comment. Auto-approvals appear only in `run_events`.
- `eval_runs` and `eval_results`: one row per eval (its configuration) and one per eval task.
  `repopilot eval import` loads the reports shipped in `eval/reports/`.
- `code_chunks`: the optional hybrid code index, kept only while its run is live.
- LangGraph's own checkpoint tables, created by `AsyncPostgresSaver.setup()`.

Migrations are numbered SQL files applied by a checksum-checked, advisory-locked runner.

## 6. Service mode

`POST /v1/runs` writes a `queued` row and enqueues a `start` job. Workers consume the Redis
Stream with a consumer group; a job that is not acknowledged within the visibility timeout is
reclaimed by another worker. Jobs are idempotent: a `start` job for a finished run does nothing,
and a redelivered one continues from the run's last checkpoint. A redelivered `resume` job
whose run already moved past the approval step, because the worker died opening the PR or
during a revision, also continues it from its checkpoint. A `resume` job for a finished run,
or a stale one for a run that is back at review, does nothing. While a job runs, the worker
renews its lease (`XCLAIM … JUSTID`) so a long run is not reclaimed by another worker.

Errors that cannot succeed on a retry fail the run at once, with the error recorded and its
sandbox released. They include an unknown task, an unsafe archive, an invalid `.repopilot.yml`,
GitHub 400/401/403/404/410/422 that are not rate limits, and sandbox refusals such as a
disallowed command or a missing image. Other errors are redelivered, and a job delivered more
than three times fails its run unless the run has finished meanwhile. A run whose sandbox was
reaped while it waited for review fails as `sandbox_expired`.

`POST /v1/runs/{id}/approval` claims the run atomically
(`UPDATE … WHERE status = 'awaiting_approval' RETURNING`), so two concurrent decisions cannot
both resume it, then enqueues a `resume` job. The decision carries the reviewer's name (`by`,
default `web`). `repopilot approve` claims the run the same way and resumes it in-process; for
a GitHub run it needs `REPOPILOT_GITHUB_TOKEN`, and it refuses before claiming if the token is
missing.

## 7. GitHub

For a GitHub issue the worker downloads the default branch's tarball and extracts it with the
standard library's `data` filter (no absolute or `..` paths, no links out of the tree, no
devices, no setuid). It imports the tree as a single commit, reads `.repopilot.yml`
(`test_command`, an allowlisted `image`, extra `protected_paths`) and starts the sandbox. The
`test_command` must start with a program the sandbox runs (`pytest`, `python`, `ruff` and so on);
anything else fails the run at once with the reason.

After approval the change is rebuilt on GitHub with the Git Data API: blobs from the sandbox's
file contents, a tree on the base commit's tree (deleted paths set to `null`), a commit, a
`repopilot/<run>` branch and a **draft** pull request with `Fixes #N`. The GitHub token is used
only on the host and never enters a sandbox.

## 8. Observability

OpenTelemetry spans are emitted per run invocation, per graph node, per model call (with GenAI
semantic-convention attributes: model, input and output tokens, finish reason) and per tool
call (outcome, denial, latency). Export is OTLP/HTTP: to Jaeger locally (`otel` compose profile)
or to Langfuse, which reads the GenAI attributes as generations. Prompt text and tool output
stay off spans unless explicitly enabled. Each run row stores its trace id. Structured run
events in Postgres remain the source of truth for the UI.

## 9. Evaluation

Benchmark tasks are bugs injected into small, realistic fixture repos. Each task has an issue
written the way a user would report it, a reference fix, and **held-out tests the agent never
sees**. `repopilot bench validate` proves every task is sound: the bug applies, the held-out
tests fail on it, and the reference fix makes them pass.

Workspaces are a single import commit with the bug already present, so the fix cannot be read
from `.git`. Scoring copies a pristine `tests/conftest.py` over the agent's before running the
held-out tests, so a planted pytest hook cannot force a pass.

A task is **resolved** when the run passed harness validation *and* the held-out tests pass.
Evals are resumable and write JSONL, Markdown and metadata reports. Rates carry 95% Wilson
intervals. The test split is frozen by per-task hashes; tuning happens on the dev split only.

## 10. Known limits

- The fixture repos are small, and every reference fix is one hunk in one file.
- Every task's visible tests pass on the buggy code, so the agent has to reproduce the problem
  from the issue. The UI and the PR body say so whenever a green suite is weak evidence.
- GitHub runs have no network inside the sandbox, so dependencies must be baked into a
  `repopilot-sandbox-*` image named in `.repopilot.yml`.
- Checkpoints are taken between steps. If a worker dies in the middle of a tools step, the
  replacement re-runs that whole step: its tool calls run again (an `apply_patch` that already
  applied then fails, and the model sees that) and are counted twice.
- Ollama reports prompt tokens without the cached prefix, so local token totals are low.
