"""Freeze the test split: tuning happens on dev, and the test numbers are measured once against
exactly the tasks (and fixture repos) that were frozen."""

import hashlib
from pathlib import Path

from repopilot.bench.tasks import REPOS_ROOT, Task
from repopilot.config import REPO_ROOT

FREEZE_FILE = REPO_ROOT / "bench" / "test_split.sha256"
_SKIP = {"__pycache__", ".pytest_cache", ".ruff_cache"}


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*")
                  if p.is_file() and not _SKIP.intersection(p.relative_to(root).parts))


def task_hash(task: Task, repos_root: Path = REPOS_ROOT) -> str:
    """sha256 over the task directory and its fixture repo, line endings normalised."""
    digest = hashlib.sha256()
    for label, root in (("task", task.dir), ("repo", repos_root / task.repo)):
        for path in _files(root):
            digest.update(f"{label}:{path.relative_to(root).as_posix()}\0".encode())
            digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
            digest.update(b"\0")
    return digest.hexdigest()


def write_freeze(tasks: list[Task], path: Path = FREEZE_FILE, repos_root: Path = REPOS_ROOT) -> None:
    lines = [f"{task_hash(t, repos_root)}  {t.id}\n" for t in sorted(tasks, key=lambda t: t.id)]
    path.write_text("".join(lines), encoding="utf-8")


def check_frozen(tasks: list[Task], path: Path = FREEZE_FILE,
                 repos_root: Path = REPOS_ROOT) -> list[str]:
    """Problems with the split against its freeze; empty when it matches exactly."""
    if not path.is_file():
        return [f"no freeze file at {path}"]
    frozen = dict(reversed(line.split(maxsplit=1)) for line in
                  path.read_text(encoding="utf-8").split("\n") if line.strip())
    frozen = {task_id.strip(): digest for task_id, digest in frozen.items()}
    problems = []
    for task in sorted(tasks, key=lambda t: t.id):
        if task.id not in frozen:
            problems.append(f"{task.id}: not in the frozen split")
        elif frozen[task.id] != task_hash(task, repos_root):
            problems.append(f"{task.id}: changed since freeze")
    current = {t.id for t in tasks}
    problems += [f"{task_id}: frozen but missing" for task_id in sorted(frozen) if task_id not in current]
    return problems
