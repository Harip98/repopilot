"""`repopilot` command line."""

import typer

from repopilot import __version__

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main() -> None:
    """RepoPilot: an AI software engineering agent."""


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


def _deps(sandbox, recorder, model: str | None):
    from repopilot.agent.deps import RunDeps
    from repopilot.agent.pr import NoPR
    from repopilot.config import get_settings
    from repopilot.llm import get_chat_model
    from repopilot.tools.builtin import build_registry

    s = get_settings()
    return RunDeps(llm=get_chat_model(model), sandbox=sandbox, registry=build_registry(),
                   recorder=recorder, pr=NoPR(), price_in=s.llm_price_in, price_out=s.llm_price_out)


def _report(result, run_id: str) -> None:
    state = result.state
    if state.plan:
        typer.echo(f"plan: {state.plan.root_cause}\n  files: {', '.join(state.plan.files)}")
    if result.interrupt:
        payload = result.interrupt
        typer.echo(f"\n--- diff ---\n{payload['diff']}--- tests: exit {payload['tests']['exit_code']} ---")
        typer.echo(payload["tests"]["output_tail"][-1500:])
        typer.echo(f"\nsummary: {payload['summary']}")
        typer.echo(f"awaiting approval: repopilot approve {run_id} [--reject --comment TEXT]")
        return
    u = state.usage
    typer.echo(f"status: {state.status}" + (f" ({state.failure_reason})" if state.failure_reason else ""))
    typer.echo(f"llm calls {u.llm_calls}, tool calls {u.tool_calls} ({u.tool_failures} failed, "
               f"{u.unauthorized_attempts} denied), tokens {u.prompt_tokens}+{u.completion_tokens}, "
               f"retries {state.retries}")


async def _finish(result, task, sandbox) -> None:
    """Grade benchmark runs once they are over, then free the sandbox."""
    from repopilot.agent.runner import grade

    if result.interrupt is not None:
        return
    if task is not None and result.state.status in ("completed", "pr_opened"):
        graded = await grade(task, sandbox, result.state.sandbox_id)
        typer.echo(f"held-out tests: {'PASS (resolved)' if graded.resolved else 'FAIL'}")
    await sandbox.destroy(result.state.sandbox_id)


@app.command()
def solve(
    task_id: str = typer.Option(..., "--task", help="Benchmark task id, e.g. orders-api-005."),
    auto_approve: bool = typer.Option(False, help="Skip the human approval step."),
    model: str | None = typer.Option(None, help="Override REPOPILOT_LLM_MODEL."),
) -> None:
    """Run the agent on a benchmark task until it needs approval or finishes."""
    from repopilot.agent.pg_recorder import PgRecorder
    from repopilot.agent.runner import (
        new_run_id,
        postgres_checkpointer,
        prepare_bench_run,
        run_async,
        start_run,
    )
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
        async with get_sandbox_client() as sandbox, postgres_checkpointer(dsn) as saver:
            deps = _deps(sandbox, recorder, model)
            run_id = new_run_id()
            state = await prepare_bench_run(task, sandbox, run_id=run_id, auto_approve=auto_approve)
            await recorder.create_run(run_id, repo=task.repo, issue_text=task.issue,
                                      task_id=task.id, issue_ref=None, model=deps.llm.model,
                                      task_mode="bench", sandbox_id=state.sandbox_id)
            typer.echo(f"run {run_id} ({task.id}, model {deps.llm.model})")
            result = await start_run(state, deps, saver)
            _report(result, run_id)
            await _finish(result, task, sandbox)
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

    import psycopg

    from repopilot.agent.pg_recorder import PgRecorder
    from repopilot.agent.runner import (
        NotAwaitingApproval,
        RunNotFound,
        postgres_checkpointer,
        resume_run,
        run_async,
    )
    from repopilot.bench.tasks import load_tasks
    from repopilot.config import get_settings
    from repopilot.sandbox_client import get_sandbox_client

    dsn = get_settings().dsn()
    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT status, model, task_id FROM runs WHERE id::text = %s",
                           (run_id,)).fetchone()
    if row is None or row[0] != "awaiting_approval":
        typer.echo(f"run {run_id} is {'unknown' if row is None else row[0]}, not awaiting approval",
                   err=True)
        raise typer.Exit(1)
    _, model, task_id = row
    task = next((t for t in load_tasks() if t.id == task_id), None)
    decision = {"decision": "reject" if reject else "approve", "comment": comment,
                "by": by or getpass.getuser()}

    async def run() -> None:
        recorder = PgRecorder(dsn)
        async with get_sandbox_client() as sandbox, postgres_checkpointer(dsn) as saver:
            try:
                result = await resume_run(run_id, decision, _deps(sandbox, recorder, model), saver)
            except (RunNotFound, NotAwaitingApproval) as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(1) from None
            _report(result, run_id)
            await _finish(result, task, sandbox)
        await recorder.close()

    run_async(run)


@app.command()
def runs(limit: int = typer.Option(20, help="How many recent runs to show.")) -> None:
    """List recent runs."""
    import psycopg

    from repopilot.config import get_settings

    with psycopg.connect(get_settings().dsn()) as conn:
        rows = conn.execute(
            "SELECT id, coalesce(task_id, repo), status, coalesce(failure_reason, ''), model,"
            " llm_calls, tool_calls, prompt_tokens + completion_tokens, created_at"
            " FROM runs ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()
    for r in rows:
        typer.echo(f"{r[0]}  {r[1]:<16} {r[2]:<18} {r[3]:<22} {r[4]:<24} "
                   f"llm={r[5]} tools={r[6]} tokens={r[7]}  {r[8]:%Y-%m-%d %H:%M}")
