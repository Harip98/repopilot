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
