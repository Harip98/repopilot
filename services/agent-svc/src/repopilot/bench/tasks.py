"""Benchmark tasks: a fixture repo, an injected bug, an issue, and held-out tests."""

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from repopilot.config import REPO_ROOT

TASKS_ROOT = REPO_ROOT / "bench" / "tasks"
REPOS_ROOT = REPO_ROOT / "bench" / "repos"
_ID = re.compile(r"^[a-z0-9-]+-\d{3}$")


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    repo: str
    title: str
    issue: str
    difficulty: Literal["simple", "complex"]
    split: Literal["dev", "test"]
    test_command: list[str] = Field(min_length=1)
    heldout_command: list[str] = Field(min_length=1)
    protected_paths: list[str] = Field(default_factory=list)
    expected_files: list[str] = Field(min_length=1)
    dir: Path = Field(default=Path(), exclude=True)

    @field_validator("id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not _ID.match(v):
            raise ValueError(f"id {v!r} must look like <repo>-NNN")
        return v

    @property
    def bug_patch(self) -> Path:
        return self.dir / "bug.patch"

    @property
    def fix_patch(self) -> Path:
        return self.dir / "fix.patch"

    @property
    def heldout_dir(self) -> Path:
        return self.dir / "heldout"


def _load_one(task_dir: Path, repos_root: Path) -> Task:
    try:
        raw = yaml.safe_load((task_dir / "task.yaml").read_text(encoding="utf-8"))
        task = Task.model_validate({**raw, "dir": task_dir})
    except (OSError, yaml.YAMLError, ValidationError, TypeError) as exc:
        raise ValueError(f"{task_dir.name}: invalid task.yaml: {exc}") from exc
    if task.id != task_dir.name:
        raise ValueError(f"{task_dir.name}: id {task.id!r} must match its directory name")
    if not (repos_root / task.repo).is_dir():
        raise ValueError(f"{task.id}: unknown repo {task.repo!r}")
    for required in (task.bug_patch, task.fix_patch):
        if not required.is_file():
            raise ValueError(f"{task.id}: missing {required.name}")
    if not task.heldout_dir.is_dir() or not any(p.is_file() for p in task.heldout_dir.rglob("*")):
        raise ValueError(f"{task.id}: heldout/ must contain the held-out tests")
    return task


def load_tasks(
    split: str | None = None, root: Path = TASKS_ROOT, repos_root: Path = REPOS_ROOT
) -> list[Task]:
    tasks = [_load_one(d, repos_root) for d in sorted(root.iterdir()) if d.is_dir()]
    return [t for t in tasks if split is None or t.split == split]


def image_for(task: Task) -> str:
    return f"repopilot-bench-{task.repo}"


COMPLEX_MIN_LINES = 3


def changed_lines(patch: str) -> int:
    """Added plus removed lines in a unified diff (file headers excluded)."""
    return sum(
        1 for ln in patch.splitlines() if ln[:1] in "+-" and not ln.startswith(("+++ ", "--- "))
    )


def difficulty_for(fix_patch: str) -> str:
    """Objective label from the reference fix: every fix in this benchmark is a single hunk,
    so "complex" just means the fix changes at least COMPLEX_MIN_LINES lines."""
    return "complex" if changed_lines(fix_patch) >= COMPLEX_MIN_LINES else "simple"
