# Running RepoPilot

This page covers setup, local models, the review console, GitHub issues, evaluation, tracing
and tests. For what the system does and why, start with the [README](../README.md).

## Prerequisites

- Docker
- [uv](https://docs.astral.sh/uv/)
- Node 22
- Either an API key for an OpenAI-compatible model, or [Ollama](https://ollama.com) for a
  local model

## Setup

```bash
cp .env.example .env                       # set the REPOPILOT_LLM_* values
docker compose up -d                       # Postgres, Redis, sandbox-svc
cd services/agent-svc
uv sync
uv run repopilot migrate
uv run repopilot bench build-images        # sandbox images for the fixture repos
uv run repopilot bench validate            # proves all 25 tasks are sound
```

Every setting is listed, with its default, in [`.env.example`](../.env.example).

## Choosing a model

**A hosted model** only needs three settings in `.env`: `REPOPILOT_LLM_BASE_URL`,
`REPOPILOT_LLM_MODEL` and `REPOPILOT_LLM_API_KEY`.

**A local model through Ollama:** build the 16k-context variants the results were measured with
([`deploy/ollama/`](../deploy/ollama/)). From the repository root:

```bash
ollama pull qwen2.5:7b-instruct
ollama create qwen2.5-7b-16k -f deploy/ollama/qwen2.5-7b-16k.Modelfile
ollama pull qwen2.5:3b-instruct            # the baseline
ollama create qwen2.5-3b-16k -f deploy/ollama/qwen2.5-3b-16k.Modelfile
```

Then make three changes to `.env`:

- set `REPOPILOT_LLM_BASE_URL=http://127.0.0.1:11434/v1`;
- set `REPOPILOT_LLM_MODEL=qwen2.5-7b-16k`;
- raise `REPOPILOT_LLM_TIMEOUT_S` (120 s by default) if model calls are slow.

The context size matters. A run's conversation (system prompt, issue, file listing and every
tool result, up to 8,000 characters each) can outgrow Ollama's default context window. When it
does, Ollama truncates the conversation instead of failing, so the model silently loses earlier
messages. Every reported run had a 16,384-token context window.

## Solve a task from the terminal

```bash
uv run repopilot bench list --split dev    # the tasks
uv run repopilot solve --task orders-api-005
uv run repopilot approve <run-id>          # or: --reject --comment "use casefold"
```

`solve` prints the plan, the diff and the test output, then exits with the run paused for
review. The run waits in the database until `approve`, or the console, decides it. Two options
change this:

- `--auto-approve` skips the review;
- `--code-index` gives the model the hybrid `find_code` search.

## The service and the review console

```bash
uv run repopilot serve                     # API on 127.0.0.1:8000
uv run repopilot worker                    # executes queued runs
cd ../../web && npm ci && npm run dev      # console on 127.0.0.1:3000
```

The console reads its own settings from `web/.env.local`; copy
[`web/.env.example`](../web/.env.example) to create it. Two settings matter:

- its API token must match `REPOPILOT_API_TOKEN` in `.env`;
- `NEXT_PUBLIC_TRACE_URL` adds an "Open trace" link to each run.

To fill the console's Evaluations page with the results shipped in
[`eval/reports/`](../eval/reports/), load them once:

```bash
uv run repopilot eval import               # idempotent; --replace reloads an eval
```

To run the API and the worker in containers instead, use
`docker compose --profile app up -d --build`. Set `REPOPILOT_CODE_INDEX=true` to give runs the
worker executes the `find_code` index.

## GitHub issues

1. Set `REPOPILOT_GITHUB_TOKEN`. Use a fine-grained token with read access to issues, and write
   access to contents and pull requests.
2. Start a run with `{"repo": "owner/name", "issue_number": 42}` from the console or the API.
3. Add a `.repopilot.yml` to the target repository with:
   - `test_command`, which must start with a program the sandbox runs, such as `pytest`;
   - `image`, a `repopilot-sandbox-*` image with the repository's dependencies baked in;
   - any extra `protected_paths`.

The sandbox has no network, so nothing is installed at run time. After approval, RepoPilot
opens a draft pull request. `repopilot approve` works for GitHub runs too, as long as the token
is set.

## Evaluation

```bash
uv run repopilot eval run --name my-dev --split dev --max-seconds 2400
uv run repopilot bench freeze --note "before final test run"
uv run repopilot eval run --name my-test --split test --max-seconds 2400
uv run repopilot eval compare my-dev my-test
```

`--max-seconds 2400` is the per-task wall-clock budget that every reported run used except dev
v1, which used the 1200 s default (see `eval/reports/*.meta.json`). The longer budget is for a
7B model running mostly on the CPU, where a single model call can take minutes.

The test split refuses to run if any frozen task has changed, including when you select tasks
with `--task`. Use a new `--name` for every run. Method and results are in
[evaluation.md](evaluation.md).

## MCP

`uv run repopilot mcp` serves the same sandboxed, policy-checked tools over stdio to any MCP
client. `REPOPILOT_MCP_ROOTS` lists the directories it may open; the default is the directory
it starts in.

## Tracing

Tracing is off unless `REPOPILOT_OTEL_ENDPOINT` is set.

- **Locally:** run `docker compose --profile otel up -d jaeger` and point the endpoint at
  `http://127.0.0.1:4318`. The Jaeger UI is on http://127.0.0.1:16686.
- **Langfuse:** use Langfuse's OTLP endpoint plus `REPOPILOT_LANGFUSE_PUBLIC_KEY` and
  `REPOPILOT_LANGFUSE_SECRET_KEY`.

Prompt text and tool output stay off spans unless `REPOPILOT_OTEL_CAPTURE_CONTENT=true`.

## Tests

```bash
cd services/agent-svc
uv run pytest                    # unit: graph, tools, policy, scoring, API models (fakes, no network)
uv run pytest -m integration     # Postgres and Redis: recorder, migrations, API, queue
uv run pytest -m sandbox         # live sandbox: isolation, tools, task validation, end-to-end solve (scripted model)
cd ../sandbox-svc && go test ./...
cd ../../web && npm run lint && npm test && npm run build
```

CI runs all of these on every push, and also builds both service images.

## Deploying

[deploy-aws.md](deploy-aws.md) sets up a single EC2 instance for a team's private use: secrets
in Parameter Store, Caddy for TLS, backups and costs.
