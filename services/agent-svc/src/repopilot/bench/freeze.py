"""Freeze the test split: tuning happens on dev, and the test numbers are measured once against
exactly the tasks (and fixture repos) that were frozen."""

import hashlib
from pathlib import Path

from repopilot.bench.tasks import REPOS_ROOT, Task
from repopilot.config import REPO_ROOT

FREEZE_FILE = REPO_ROOT / "bench" / "test_split.sha256"
# The same files the workspace builder leaves out.
_SKIP = {"__pycache__", ".pytest_cache", ".ruff_cache", ".venv"}


def _files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and p.suffix != ".pyc" and not _SKIP.intersection(p.relative_to(root).parts)
    )


def task_hash(task: Task, repos_root: Path = REPOS_ROOT) -> str:
    """sha256 over the task directory, its fixture repo and the sandbox images it runs in
    (they pin pytest and the cleanup tools), line endings normalised."""
    digest = hashlib.sha256()
    images = repos_root.parent / "images"
    for label, root in (
        ("task", task.dir),
        ("repo", repos_root / task.repo),
        ("base-image", images / "base"),
        ("image", images / task.repo),
    ):
        for path in _files(root):
            digest.update(f"{label}:{path.relative_to(root).as_posix()}\0".encode())
            digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
            digest.update(b"\0")
    return digest.hexdigest()


def write_freeze(
    tasks: list[Task], path: Path = FREEZE_FILE, repos_root: Path = REPOS_ROOT
) -> None:
    lines = [f"{task_hash(t, repos_root)}  {t.id}\n" for t in sorted(tasks, key=lambda t: t.id)]
    path.write_text("".join(lines), encoding="utf-8", newline="\n")


def check_frozen(
    tasks: list[Task],
    path: Path = FREEZE_FILE,
    repos_root: Path = REPOS_ROOT,
    *,
    subset: bool = False,
) -> list[str]:
    """Problems with the split against its freeze; empty when it matches exactly. With
    `subset`, `tasks` is a selection from the split: each must match its freeze, and the
    frozen tasks left out are not reported missing."""
    if not path.is_file():
        return [f"no freeze file at {path}"]
    frozen = dict(
        reversed(line.split(maxsplit=1))
        for line in path.read_text(encoding="utf-8").split("\n")
        if line.strip()
    )
    frozen = {task_id.strip(): digest for task_id, digest in frozen.items()}
    problems = []
    for task in sorted(tasks, key=lambda t: t.id):
        if task.id not in frozen:
            problems.append(f"{task.id}: not in the frozen split")
        elif frozen[task.id] != task_hash(task, repos_root):
            problems.append(f"{task.id}: changed since freeze")
    if not subset:
        current = {t.id for t in tasks}
        problems += [
            f"{task_id}: frozen but missing" for task_id in sorted(frozen) if task_id not in current
        ]
    return problems
