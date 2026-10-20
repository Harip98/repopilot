import subprocess
from pathlib import Path

import pytest

from repopilot.bench.tasks import image_for, load_tasks
from repopilot.bench.workspace import build_base_workspace

TASK_YAML = """\
id: demo-001
repo: demo
title: add() subtracts
issue: |
  Calling add(2, 3) returns -1.
commit_message: Simplify arithmetic helpers
difficulty: simple
split: dev
test_command: [pytest, -q]
heldout_command: [pytest, -q, tests/test_heldout_add.py]
protected_paths: ["tests/test_heldout_*"]
expected_files: [calc.py]
"""
BUG = """\
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a + b
+    return a - b
"""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def bench(tmp_path: Path) -> Path:
    repo = tmp_path / "repos" / "demo"
    repo.mkdir(parents=True)
    (repo / "calc.py").write_bytes(b"def add(a, b):\n    return a + b\n")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "calc.cpython-312.pyc").write_bytes(b"\0")
    task = tmp_path / "tasks" / "demo-001"
    (task / "heldout" / "tests").mkdir(parents=True)
    (task / "task.yaml").write_text(TASK_YAML)
    (task / "bug.patch").write_bytes(BUG.encode())
    (task / "fix.patch").write_bytes(b"")
    (task / "heldout" / "tests" / "test_heldout_add.py").write_text("def test_x():\n    pass\n")
    return tmp_path


def test_load_tasks(bench: Path):
    [task] = load_tasks(root=bench / "tasks", repos_root=bench / "repos")
    assert task.id == "demo-001" and task.repo == "demo" and task.difficulty == "simple"
    assert task.test_command == ["pytest", "-q"] and task.dir == bench / "tasks" / "demo-001"
    assert image_for(task) == "repopilot-bench-demo"
    assert load_tasks(split="test", root=bench / "tasks", repos_root=bench / "repos") == []


@pytest.mark.parametrize("breakage, needle", [
    (lambda t: (t / "task.yaml").write_text(TASK_YAML.replace("demo-001", "Demo_1")), "id"),
    (lambda t: (t / "bug.patch").unlink(), "bug.patch"),
    (lambda t: (t / "task.yaml").write_text(TASK_YAML.replace("repo: demo", "repo: nope")), "nope"),
    (lambda t: (t / "task.yaml").write_text(TASK_YAML.replace("split: dev", "split: train")), "split"),
])
def test_load_tasks_validates(bench: Path, breakage, needle):
    breakage(bench / "tasks" / "demo-001")
    with pytest.raises(ValueError, match=needle):
        load_tasks(root=bench / "tasks", repos_root=bench / "repos")


def test_build_base_workspace_history(bench: Path, tmp_path: Path):
    [task] = load_tasks(root=bench / "tasks", repos_root=bench / "repos")
    ws = build_base_workspace(task, tmp_path / "ws", repos_root=bench / "repos")
    assert _git(ws, "log", "--format=%s").splitlines() == ["Simplify arithmetic helpers", "initial"]
    assert _git(ws, "status", "--porcelain") == ""
    assert (ws / "calc.py").read_bytes() == b"def add(a, b):\n    return a - b\n"
    assert not (ws / "__pycache__").exists()
    assert not list(ws.rglob("test_heldout_*"))
