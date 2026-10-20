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
