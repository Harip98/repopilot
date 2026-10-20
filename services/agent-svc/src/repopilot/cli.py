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
                deps = _deps(sandbox, recorder, model)
                run_id = new_run_id()
                typer.echo(f"run {run_id} ({task.id}, model {deps.llm.model})")
                outcome = await run_bench_task(task, deps, saver, run_id=run_id,
                                               auto_approve=auto_approve)
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
