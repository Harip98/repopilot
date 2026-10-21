from pathlib import Path

from repopilot.bench.freeze import check_frozen, task_hash, write_freeze
from repopilot.bench.tasks import Task


def make_task(root: Path, task_id: str, body: bytes = b"print(1)\n") -> Task:
    repo = root / "repos" / "demo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "app.py").write_bytes(body)
    task_dir = root / "tasks" / task_id
    (task_dir / "heldout").mkdir(parents=True, exist_ok=True)
    (task_dir / "task.yaml").write_bytes(b"id: x\n")
    (task_dir / "heldout" / "test_h.py").write_bytes(b"def test(): pass\n")
    return Task(id=task_id, repo="demo", title="t", issue="i", difficulty="simple", split="test",
                test_command=["pytest"], heldout_command=["pytest"], expected_files=["app.py"],
                dir=task_dir)


def test_split_hash_stable(tmp_path: Path):
    lf = make_task(tmp_path / "a", "demo-001", b"x = 1\ny = 2\n")
    crlf = make_task(tmp_path / "b", "demo-001", b"x = 1\r\ny = 2\r\n")
    assert task_hash(lf, repos_root=tmp_path / "a" / "repos") == task_hash(
        crlf, repos_root=tmp_path / "b" / "repos")
    other = make_task(tmp_path / "c", "demo-001", b"x = 1\ny = 3\n")
    assert task_hash(other, repos_root=tmp_path / "c" / "repos") != task_hash(
        lf, repos_root=tmp_path / "a" / "repos")


def test_frozen_split_guard(tmp_path: Path):
    repos = tmp_path / "repos"
    freeze = tmp_path / "test_split.sha256"
    tasks = [make_task(tmp_path, "demo-001"), make_task(tmp_path, "demo-002")]
    assert check_frozen(tasks, freeze, repos_root=repos) == [f"no freeze file at {freeze}"]
    write_freeze(tasks, freeze, repos_root=repos)
    assert check_frozen(tasks, freeze, repos_root=repos) == []

    (tasks[1].dir / "heldout" / "test_h.py").write_bytes(b"def test(): assert 0\n")
    extra = make_task(tmp_path, "demo-003")
    problems = check_frozen([tasks[1], extra], freeze, repos_root=repos)
    assert problems == ["demo-002: changed since freeze", "demo-003: not in the frozen split",
                        "demo-001: frozen but missing"]

    (repos / "demo" / "app.py").write_bytes(b"changed\n")  # the fixture repo is hashed too
    assert "demo-001: changed since freeze" in check_frozen(tasks, freeze, repos_root=repos)


def test_sandbox_images_are_hashed(tmp_path: Path):
    task = make_task(tmp_path, "demo-001")
    repos = tmp_path / "repos"
    for image in ("base", "demo"):
        (tmp_path / "images" / image).mkdir(parents=True)
        (tmp_path / "images" / image / "Dockerfile").write_bytes(b"FROM python:3.12-slim\n")
    before = task_hash(task, repos_root=repos)
    (tmp_path / "images" / "base" / "Dockerfile").write_bytes(b"FROM python:3.13-slim\n")
    after_base = task_hash(task, repos_root=repos)
    (tmp_path / "images" / "demo" / "Dockerfile").write_bytes(b"FROM x\n")
    assert len({before, after_base, task_hash(task, repos_root=repos)}) == 3


def test_local_environments_are_not_hashed(tmp_path: Path):
    task = make_task(tmp_path, "demo-001")
    repos = tmp_path / "repos"
    before = task_hash(task, repos_root=repos)
    (repos / "demo" / ".venv").mkdir()
    (repos / "demo" / ".venv" / "pyvenv.cfg").write_bytes(b"home = x\n")
    (repos / "demo" / "app.cpython-312.pyc").write_bytes(b"\0")
    assert task_hash(task, repos_root=repos) == before
