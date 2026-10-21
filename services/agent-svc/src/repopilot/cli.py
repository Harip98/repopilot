"""`repopilot` command line."""

from typing import Annotated

import typer

from repopilot import __version__

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main(ctx: typer.Context) -> None:
    """RepoPilot: an AI software engineering agent."""
    import atexit

    from repopilot.config import get_settings
    from repopilot.telemetry import setup_tracing

    provider = setup_tracing(get_settings(), f"repopilot-{ctx.invoked_subcommand or 'cli'}")
    if provider is not None:
        atexit.register(provider.shutdown)  # flush buffered spans before the process exits


@app.command()
def version() -> None:
    """Print the RepoPilot version."""
    typer.echo(__version__)


@app.command()
def migrate() -> None:
    """Apply pending database migrations."""
    import psycopg

    from repopilot.config import get_settings
    from repopilot.db.migrate import apply_migrations

    with psycopg.connect(get_settings().dsn(), autocommit=True) as conn:
        applied = apply_migrations(conn)
    typer.echo(f"applied {', '.join(applied)}" if applied else "up to date")


bench_app = typer.Typer(no_args_is_help=True, help="Benchmark tasks and fixture images.")
app.add_typer(bench_app, name="bench")


@bench_app.command("list")
def bench_list(split: str | None = typer.Option(None, help="dev or test")) -> None:
    """List benchmark tasks."""
    from repopilot.bench.tasks import load_tasks

    for task in load_tasks(split=split):
        typer.echo(f"{task.id}\t{task.split}\t{task.difficulty}\t{task.title}")


@bench_app.command("build-images")
def bench_build_images() -> None:
    """Build the sandbox base image, then every fixture image on top of it."""
    import subprocess

    from repopilot.config import REPO_ROOT

    images = REPO_ROOT / "bench" / "images"
    order = ["base"] + sorted(d.name for d in images.iterdir() if d.is_dir() and d.name != "base")
    for name in order:
        tag = "repopilot-sandbox-base" if name == "base" else f"repopilot-bench-{name}"
        typer.echo(f"building {tag}")
        subprocess.run(["docker", "build", "-q", "-t", tag, str(images / name)], check=True)


@bench_app.command("validate")
def bench_validate(task_id: str | None = typer.Option(None, "--task", help="Only this task.")) -> None:
    """Check every task: bug applies, held-out tests fail on it, reference fix passes them."""
    import anyio

    from repopilot.bench.tasks import load_tasks
    from repopilot.bench.validate import validate_task
    from repopilot.sandbox_client import get_sandbox_client

    tasks = [t for t in load_tasks() if task_id in (None, t.id)]

    async def run() -> bool:
        ok = True
        async with get_sandbox_client() as sandbox:
            for task in tasks:
                r = await validate_task(task, sandbox)
                ok &= r.valid
                typer.echo(f"{task.id}\t{'valid' if r.valid else 'INVALID'}\t"
                           f"visible_on_base={'pass' if r.visible_pass_on_base else 'fail'}"
                           + ("" if r.valid else f"\n{r.detail}"))
        return ok

    if not anyio.run(run):
        raise typer.Exit(1)


def _deps(sandbox, recorder, model: str | None, code_search=None):
    from repopilot.agent.deps import RunDeps
    from repopilot.agent.pr import NoPR
    from repopilot.config import get_settings
    from repopilot.llm import get_chat_model
    from repopilot.tools.builtin import build_registry

    s = get_settings()
    return RunDeps(llm=get_chat_model(model), sandbox=sandbox,
                   registry=build_registry(find_code_tool=code_search is not None),
                   recorder=recorder, pr=NoPR(), price_in=s.llm_price_in, price_out=s.llm_price_out,
                   code_search=code_search)


def _code_index_hooks(enabled: bool, run_id: str):
    """(code_search, on_workspace) for a run with the hybrid code index, or (None, None)."""
    if not enabled:
        return None, None
    from repopilot.code_index import CodeIndex
    from repopilot.config import get_settings
    from repopilot.llm.embeddings import OpenAICompatEmbedder

    s = get_settings()
    index = CodeIndex(s.dsn(), OpenAICompatEmbedder(s.embed_base_url or s.llm_base_url,
                                                    s.llm_api_key.get_secret_value(), s.embed_model))

    async def search(query: str):
        return await index.search(run_id, query)

    async def build(workspace) -> None:
        await index.index(run_id, workspace)

    return search, build


def _report(result, run_id: str) -> None:
    state = result.state
    if state.plan:
        typer.echo(f"plan: {state.plan.root_cause}\n  files: {', '.join(state.plan.files)}")
    if result.interrupt:
        payload = result.interrupt
        typer.echo(f"\n--- diff ---\n{payload['diff']}--- tests: exit {payload['tests']['exit_code']} ---")
        typer.echo(payload["tests"]["output_tail"][-1500:])
        if payload["tests"].get("passed_before_change"):
            typer.echo("note: the test suite also passed before this change, so a green run "
                       "alone does not show the issue is fixed")
        typer.echo(f"\nsummary: {payload['summary']}")
        typer.echo(f"awaiting approval: repopilot approve {run_id} [--reject --comment TEXT]")
        typer.echo("the sandbox is reaped 2 h after the run started; approve before then")
        return
    u = state.usage
    typer.echo(f"status: {state.status}" + (f" ({state.failure_reason})" if state.failure_reason else ""))
    typer.echo(f"llm calls {u.llm_calls}, tool calls {u.tool_calls} ({u.tool_failures} failed, "
               f"{u.unauthorized_attempts} denied), tokens {u.prompt_tokens}+{u.completion_tokens}, "
               f"retries {state.retries}")


def _report_grade(graded) -> None:
    if graded is not None:
        typer.echo(f"held-out tests: {'PASS (resolved)' if graded.resolved else 'FAIL'}")


@app.command()
def solve(
    task_id: str = typer.Option(..., "--task", help="Benchmark task id, e.g. orders-api-005."),
    auto_approve: bool = typer.Option(False, help="Skip the human approval step."),
    model: str | None = typer.Option(None, help="Override REPOPILOT_LLM_MODEL."),
    code_index: bool = typer.Option(False, help="Index the workspace and give the agent find_code."),
) -> None:
    """Run the agent on a benchmark task until it needs approval or finishes."""
    from repopilot.agent.pg_recorder import PgRecorder
    from repopilot.agent.runner import new_run_id, postgres_checkpointer, run_async, run_bench_task
    from repopilot.bench.tasks import load_tasks
    from repopilot.config import get_settings
    from repopilot.sandbox_client import get_sandbox_client

    task = next((t for t in load_tasks() if t.id == task_id), None)
    if task is None:
        typer.echo(f"unknown task {task_id}", err=True)
        raise typer.Exit(1)

    async def run() -> None:
        dsn = get_settings().dsn()
        recorder = PgRecorder(dsn)
        try:
            async with get_sandbox_client() as sandbox, postgres_checkpointer(dsn) as saver:
                run_id = new_run_id()
                search, build = _code_index_hooks(code_index, run_id)
                deps = _deps(sandbox, recorder, model, search)
                typer.echo(f"run {run_id} ({task.id}, model {deps.llm.model})")
                outcome = await run_bench_task(task, deps, saver, run_id=run_id,
                                               auto_approve=auto_approve, on_workspace=build)
                _report(outcome.result, run_id)
                _report_grade(outcome.graded)
        finally:
            await recorder.close()

    run_async(run)


@app.command()
def approve(
    run_id: str,
    reject: bool = typer.Option(False, help="Reject instead of approving."),
    comment: str | None = typer.Option(None, help="Feedback; a rejection with a comment gets one revision."),
    by: str | None = typer.Option(None, help="Reviewer name (default: current user)."),
) -> None:
    """Approve or reject a run that is waiting for review."""
    import getpass

    from repopilot.agent.pg_recorder import PgRecorder
    from repopilot.agent.runner import (
        NotAwaitingApproval,
        RunNotFound,
        grade,
        postgres_checkpointer,
        resume_run,
        run_async,
    )
    from repopilot.bench.tasks import load_tasks
    from repopilot.config import get_settings
    from repopilot.sandbox_client import SandboxError, get_sandbox_client

    dsn = get_settings().dsn()
    decision = {"decision": "reject" if reject else "approve", "comment": comment,
                "by": by or getpass.getuser()}

    async def run() -> bool:
        recorder = PgRecorder(dsn)
        try:
            # Claim first: of two concurrent `approve` commands only one may resume the run.
            claimed = await recorder.claim_for_approval(run_id)
            if claimed is None:
                typer.echo(f"run {run_id} is unknown or not awaiting approval", err=True)
                return False
            task = next((t for t in load_tasks() if t.id == claimed["task_id"]), None)
            async with get_sandbox_client() as sandbox, postgres_checkpointer(dsn) as saver:
                result = None
                try:
                    try:
                        await sandbox.exec(claimed["sandbox_id"], ["git", "--version"])
                    except SandboxError:
                        await recorder.status(run_id, "failed", failure_reason="sandbox_expired")
                        typer.echo("the run's sandbox has expired; start a new run", err=True)
                        return False
                    try:
                        result = await resume_run(run_id, decision,
                                                  _deps(sandbox, recorder, claimed["model"]), saver)
                    except (RunNotFound, NotAwaitingApproval) as exc:
                        typer.echo(str(exc), err=True)
                        return False
                    _report(result, run_id)
                    if (task is not None and result.interrupt is None
                            and result.state.status in ("completed", "pr_opened")):
                        _report_grade(await grade(task, sandbox, claimed["sandbox_id"]))
                except Exception as exc:
                    await recorder.status(run_id, "failed", failure_reason="error",
                                          error=f"{type(exc).__name__}: {exc}"[:2000])
                    raise
                finally:
                    if result is None or result.interrupt is None:
                        try:
                            await sandbox.destroy(claimed["sandbox_id"])
                        except SandboxError:
                            pass
            return True
        finally:
            await recorder.close()

    if not run_async(run):
        raise typer.Exit(1)


@app.command()
def runs(limit: int = typer.Option(20, help="How many recent runs to show.")) -> None:
    """List recent runs."""
    import psycopg

    from repopilot.config import get_settings

    with psycopg.connect(get_settings().dsn()) as conn:
        rows = conn.execute(
            "SELECT id, coalesce(task_id, repo), status, coalesce(failure_reason, ''), coalesce(model, ''),"
            " llm_calls, tool_calls, prompt_tokens + completion_tokens, created_at"
            " FROM runs ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()
    for r in rows:
        typer.echo(f"{r[0]}  {r[1]:<16} {r[2]:<18} {r[3]:<22} {r[4]:<24} "
                   f"llm={r[5]} tools={r[6]} tokens={r[7]}  {r[8]:%Y-%m-%d %H:%M}")


@bench_app.command("freeze")
def bench_freeze(note: str = typer.Option(..., help="Why the split is being (re)frozen.")) -> None:
    """Freeze the test split: record a hash per test task and its fixture repo."""
    from datetime import UTC, datetime

    from repopilot.bench.freeze import FREEZE_FILE, write_freeze
    from repopilot.bench.tasks import load_tasks

    tasks = load_tasks(split="test")
    write_freeze(tasks)
    log = FREEZE_FILE.parent / "FREEZE_LOG.md"
    if not log.exists():
        log.write_text("# Test split freezes\n\n| Date (UTC) | Tasks | Note |\n|---|---|---|\n",
                       encoding="utf-8")
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"| {datetime.now(UTC):%Y-%m-%d %H:%M} | {len(tasks)} | {note} |\n")
    typer.echo(f"froze {len(tasks)} test tasks into {FREEZE_FILE.name}")


eval_app = typer.Typer(no_args_is_help=True, help="Run and report evaluations.")
app.add_typer(eval_app, name="eval")


def _eval_tasks(split: str):
    from repopilot.bench.tasks import load_tasks

    if split not in ("dev", "test", "all"):
        raise typer.BadParameter("split must be dev, test or all")
    return load_tasks(split=None if split == "all" else split)


@eval_app.command("run")
def eval_run(
    name: str = typer.Option(..., help="Eval name; rerunning the same name resumes it."),
    split: str = typer.Option("dev", help="dev, test or all"),
    model: str | None = typer.Option(None, help="Override REPOPILOT_LLM_MODEL."),
    task_id: Annotated[list[str] | None, typer.Option("--task", help="Only these task ids.")] = None,
    allow_unfrozen: bool = typer.Option(False, help="Run the test split even if it changed."),
    max_seconds: float = typer.Option(1200.0, help="Per-task wall-clock budget (slow local GPUs need more)."),
    code_index: bool = typer.Option(False, help="Give the agent the hybrid find_code index."),
) -> None:
    """Run the agent with auto-approval over a split and write eval/reports/<name>.*"""
    from repopilot.agent.pg_recorder import PgRecorder
    from repopilot.agent.runner import postgres_checkpointer, run_async, run_bench_task
    from repopilot.agent.state import Budget
    from repopilot.bench.freeze import check_frozen
    from repopilot.config import get_settings
    from repopilot.eval.runner import PgResultStore, eval_meta, run_eval, write_report
    from repopilot.sandbox_client import get_sandbox_client

    tasks = [t for t in _eval_tasks(split) if not task_id or t.id in task_id]
    if split in ("test", "all") and not allow_unfrozen:
        problems = check_frozen([t for t in tasks if t.split == "test"]) if not task_id else []
        if problems:
            typer.echo("the test split differs from its freeze:\n  " + "\n  ".join(problems), err=True)
            raise typer.Exit(1)
    settings = get_settings()
    budget = Budget(max_seconds=max_seconds)

    async def run() -> None:
        dsn = settings.dsn()
        recorder = PgRecorder(dsn)
        store = PgResultStore(dsn)
        try:
            async with get_sandbox_client() as sandbox, postgres_checkpointer(dsn) as saver:
                model_name = _deps(sandbox, recorder, model).llm.model

                async def run_task(task, run_id):
                    search, build = _code_index_hooks(code_index, run_id)
                    deps = _deps(sandbox, recorder, model, search)
                    return await run_bench_task(task, deps, saver, run_id=run_id,
                                                auto_approve=True, budget=budget,
                                                on_workspace=build)

                def progress(r) -> None:
                    typer.echo(f"{r.task_id:<16} {'RESOLVED' if r.resolved else 'unresolved':<11}"
                               f"{r.status:<10} {r.failure_reason or r.error or ''}"[:160])

                meta = eval_meta(name, model_name, settings.llm_base_url, tasks,
                                 budget.model_dump(include={"max_llm_calls", "max_tool_calls",
                                                            "max_seconds", "max_retries"}))
                results = await run_eval(name, tasks, run_task, store, model=model_name,
                                         on_result=progress)
            path = write_report(name, results, meta)
            typer.echo(f"report: {path}")
        finally:
            await recorder.close()

    run_async(run)


@eval_app.command("report")
def eval_report(name: str) -> None:
    """Re-render eval/reports/<name>.* from the stored results."""
    import json

    from repopilot.agent.runner import run_async
    from repopilot.config import get_settings
    from repopilot.eval.runner import REPORTS_DIR, PgResultStore, write_report

    results = run_async(PgResultStore(get_settings().dsn()).load, name)
    if not results:
        typer.echo(f"no results for {name}", err=True)
        raise typer.Exit(1)
    meta_path = REPORTS_DIR / f"{name}.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    typer.echo(f"report: {write_report(name, results, meta)}")


@eval_app.command("compare")
def eval_compare(names: list[str]) -> None:
    """Side-by-side summary of two or more evals."""
    from repopilot.agent.runner import run_async
    from repopilot.config import get_settings
    from repopilot.eval.runner import PgResultStore
    from repopilot.eval.score import summarize

    store = PgResultStore(get_settings().dsn())
    summaries = [summarize(run_async(store.load, n)) for n in names]

    def pct(v):
        return "n/a" if v is None else f"{v * 100:.1f}%"

    def num(v, fmt="{:,.0f}"):
        return "n/a" if v is None else fmt.format(v)

    rows = [
        ("Tasks", [str(s.n) for s in summaries]),
        ("Resolved", [f"{s.resolved} ({pct(s.resolution_rate)})" for s in summaries]),
        ("95% CI", [f"{pct(s.resolution_ci[0])}–{pct(s.resolution_ci[1])}" for s in summaries]),
        ("Patch applied", [pct(s.patch_applied_rate) for s in summaries]),
        ("Tool failure rate", [pct(s.tool_failure_rate) for s in summaries]),
        ("Avg retries", [num(s.avg_retries, "{:.2f}") for s in summaries]),
        ("p50 / p95 s", [f"{num(s.p50_latency_s)} / {num(s.p95_latency_s)}" for s in summaries]),
        ("Tokens / resolved", [num(s.tokens_per_resolved) for s in summaries]),
        ("Unauthorized", [str(s.unauthorized_attempts) for s in summaries]),
        ("Harness errors", [str(s.errors) for s in summaries]),
    ]
    typer.echo("| Metric | " + " | ".join(names) + " |")
    typer.echo("|---|" + "---|" * len(names))
    for label, values in rows:
        typer.echo(f"| {label} | " + " | ".join(values) + " |")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address."),
    port: int = typer.Option(8000, help="Port."),
) -> None:
    """Run the HTTP API (runs, SSE timeline, approvals, evals)."""
    import uvicorn

    from repopilot.agent.pg_recorder import PgRecorder
    from repopilot.agent.runner import run_async
    from repopilot.api import ApiServices, create_app
    from repopilot.api.repo import RunsRepo
    from repopilot.bench.tasks import load_tasks
    from repopilot.config import get_settings
    from repopilot.eval.runner import PgResultStore
    from repopilot.queue import JobQueue

    s = get_settings()
    tasks = {t.id: t for t in load_tasks()}
    app_ = create_app(ApiServices(
        repo=RunsRepo(s.dsn()), claims=PgRecorder(s.dsn()), queue=JobQueue(s.redis_url),
        results=PgResultStore(s.dsn()), load_task=tasks.get,
        token=s.api_token.get_secret_value(), ui_origin=s.ui_origin, default_model=s.llm_model))
    # Served on our own event loop: psycopg's async mode needs a selector loop on Windows.
    server = uvicorn.Server(uvicorn.Config(app_, host=host, port=port, log_level="info"))
    run_async(server.serve)


@app.command()
def worker(name: str = typer.Option("worker-1", help="Consumer name in the Redis group.")) -> None:
    """Execute queued runs: start, continue after a crash, and resume after approval."""
    import logging

    import anyio

    from repopilot.agent.deps import RunDeps
    from repopilot.agent.pg_recorder import PgRecorder
    from repopilot.agent.pr import NoPR
    from repopilot.agent.runner import postgres_checkpointer, run_async
    from repopilot.bench.tasks import load_tasks
    from repopilot.config import get_settings
    from repopilot.github import GitHubClient, GitHubPR, prepare_github_run
    from repopilot.llm import get_chat_model
    from repopilot.queue import JobQueue
    from repopilot.sandbox_client import get_sandbox_client
    from repopilot.tools.builtin import build_registry
    from repopilot.worker import Worker, WorkerServices, handle_job

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    s = get_settings()
    tasks = {t.id: t for t in load_tasks()}
    token = s.github_token.get_secret_value()

    async def run() -> None:
        recorder = PgRecorder(s.dsn())
        queue = JobQueue(s.redis_url)
        github = GitHubClient(token, s.github_api_url) if token else None
        try:
            async with get_sandbox_client() as sandbox, postgres_checkpointer(s.dsn()) as saver:
                def deps_for(model: str | None) -> RunDeps:
                    return RunDeps(
                        llm=get_chat_model(model), sandbox=sandbox, registry=build_registry(),
                        recorder=recorder, pr=GitHubPR(github, sandbox) if github else NoPR(),
                        price_in=s.llm_price_in, price_out=s.llm_price_out)

                async def prepare_github(run: dict, deps: RunDeps):
                    if github is None:
                        raise RuntimeError("REPOPILOT_GITHUB_TOKEN is not set")
                    return await prepare_github_run(run, run_id=run["run_id"], github=github,
                                                    sandbox=sandbox,
                                                    auto_approve=run.get("auto_approve", False))

                services = WorkerServices(deps_for=deps_for, checkpointer=saver,
                                          recorder=recorder, load_task=tasks.get,
                                          prepare_github=prepare_github)

                async def handle(job: dict) -> None:
                    await handle_job(job, services)

                async def exhausted(job: dict) -> None:
                    await recorder.status(job["run_id"], "failed",
                                          failure_reason="job_retries_exhausted")

                typer.echo(f"worker {name} consuming {queue.stream}")
                await Worker(queue, handle, name, visibility_ms=s.job_visibility_s * 1000,
                             on_exhausted=exhausted).run_forever(anyio.Event())
        finally:
            await recorder.close()
            await queue.close()
            if github:
                await github.aclose()

    run_async(run)


@app.command("mcp")
def mcp_command() -> None:
    """Serve RepoPilot's sandboxed tools over MCP (stdio), e.g. for Claude Code."""
    from repopilot.agent.runner import run_async
    from repopilot.mcp_server import WorkspaceTools, serve_stdio
    from repopilot.sandbox_client import get_sandbox_client

    async def run() -> None:
        async with get_sandbox_client() as sandbox:
            await serve_stdio(WorkspaceTools(sandbox))

    run_async(run)
