"""Build a task's starting workspace on the host: the fixture repo with the bug committed.

The bug lands as its own commit with a plausible message, so `git log` shows the kind of
history a real investigation starts from. Held-out tests are never copied in.
"""

import os
import shutil
import subprocess
from pathlib import Path

from repopilot.bench.tasks import REPOS_ROOT, Task

_IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "*.pyc")
# Fixed identities and dates keep workspaces reproducible; autocrlf off keeps Windows hosts
# from rewriting line endings.
_GIT = ["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf", "-c", "commit.gpgsign=false"]


def _git(cwd: Path, *args: str, date: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Orders Team", "GIT_AUTHOR_EMAIL": "dev@example.com",
        "GIT_COMMITTER_NAME": "Orders Team", "GIT_COMMITTER_EMAIL": "dev@example.com",
        "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date,
    }
    subprocess.run([*_GIT, *args], cwd=cwd, env=env, check=True, capture_output=True)


def build_base_workspace(task: Task, dest: Path, repos_root: Path = REPOS_ROOT) -> Path:
    shutil.copytree(repos_root / task.repo, dest, ignore=_IGNORE)
    _git(dest, "init", "-q", "-b", "main", date="2026-09-01T09:00:00+00:00")
    _git(dest, "add", "-A", date="2026-09-01T09:00:00+00:00")
    _git(dest, "commit", "-q", "-m", "initial", date="2026-09-01T09:00:00+00:00")
    if task.bug_patch.stat().st_size:
        _git(dest, "apply", "--whitespace=nowarn", str(task.bug_patch.resolve()),
             date="2026-09-15T14:30:00+00:00")
        _git(dest, "add", "-A", date="2026-09-15T14:30:00+00:00")
        _git(dest, "commit", "-q", "-m", task.commit_message, date="2026-09-15T14:30:00+00:00")
    return dest
