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
