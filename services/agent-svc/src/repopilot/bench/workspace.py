"""Build a task's starting workspace on the host: the fixture repo with the bug already in it.

The workspace has a single import commit. Earlier designs committed the bug on top of a clean
commit, which left the exact fix in `.git` (revert HEAD) for any agent that ran `git show`.
Held-out tests are never copied in.
"""

import os
import shutil
import subprocess
from pathlib import Path

from repopilot.bench.tasks import REPOS_ROOT, Task

_IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "*.pyc")
_DATE = "2026-09-01T09:00:00+00:00"
# The repo is built on the host but used by Linux git in the sandbox: ignore host config and
# templates, and pin the settings a Windows `git init` would otherwise write.
_GIT = ["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf", "-c", "commit.gpgsign=false"]
_LOCAL_CONFIG = {"core.symlinks": "true", "core.filemode": "true", "core.ignorecase": "false",
                 "core.autocrlf": "false"}


def _git(cwd: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Orders Team", "GIT_AUTHOR_EMAIL": "dev@example.com",
        "GIT_COMMITTER_NAME": "Orders Team", "GIT_COMMITTER_EMAIL": "dev@example.com",
        "GIT_AUTHOR_DATE": _DATE, "GIT_COMMITTER_DATE": _DATE,
    }
    proc = subprocess.run([*_GIT, *args], cwd=cwd, env=env, capture_output=True, text=True,
                          check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")


def build_base_workspace(task: Task, dest: Path, repos_root: Path = REPOS_ROOT) -> Path:
    shutil.copytree(repos_root / task.repo, dest, ignore=_IGNORE)
    _git(dest, "init", "-q", "-b", "main", "--template=")
    for key, value in _LOCAL_CONFIG.items():
        _git(dest, "config", "--local", key, value)
    if task.bug_patch.stat().st_size:
        _git(dest, "apply", "--whitespace=nowarn", str(task.bug_patch.resolve()))
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", f"Import {task.repo}")
    return dest
