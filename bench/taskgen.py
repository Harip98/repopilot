"""Shared machinery for the task generators.

Each task is an exact text replacement on a clean fixture repo: bug.patch is the clean ->
buggy diff, fix.patch its reverse (the minimal reference fix). heldout/ gets the task's
held-out test plus a pristine tests/conftest.py, which scoring copies over the workspace.
After generating, run `repopilot bench validate`.
"""

import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

BENCH = Path(__file__).resolve().parent
GIT = ["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf"]
# Held-out test names plus the test infrastructure that decides how pytest runs them.
PROTECTED = ('["tests/test_heldout_*", "conftest.py", "*/conftest.py", "pyproject.toml", '
             '"pytest.ini", ".pytest.ini", "setup.cfg", "tox.ini"]')
# Objective difficulty, mirrored in repopilot.bench.tasks.difficulty_for: every reference fix
# is a single hunk, so "complex" means it changes at least this many lines.
COMPLEX_MIN_LINES = 3


def difficulty_for(fix: str) -> str:
    changed = sum(1 for ln in fix.splitlines()
                  if ln[:1] in "+-" and not ln.startswith(("+++ ", "--- ")))
    return "complex" if changed >= COMPLEX_MIN_LINES else "simple"


def diff_for(repo: Path, edits: list[tuple[str, str, str]]) -> tuple[str, str]:
    """(bug.patch, fix.patch) for exact replacements; each `old` must occur exactly once."""
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "ws"
        shutil.copytree(repo, ws, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        subprocess.run([*GIT, "init", "-q"], cwd=ws, check=True)
        subprocess.run([*GIT, "add", "-A"], cwd=ws, check=True)
        subprocess.run([*GIT, "-c", "user.name=g", "-c", "user.email=g@x", "commit", "-qm", "c"],
                       cwd=ws, check=True)
        for rel, old, new in edits:
            path = ws / rel
            text = path.read_text(encoding="utf-8")
            assert text.count(old) == 1, (rel, old)
            path.write_bytes(text.replace(old, new).encode())
        bug = subprocess.run([*GIT, "diff"], cwd=ws, check=True, capture_output=True).stdout
        fix = subprocess.run([*GIT, "diff", "-R"], cwd=ws, check=True, capture_output=True).stdout
        return bug.decode(), fix.decode()


def _yaml_block(text: str) -> str:
    return "\n".join("  " + ln if ln else "" for ln in textwrap.dedent(text).rstrip().splitlines())


def write_tasks(repo_name: str, specs: dict[str, dict], test_command: str = "[pytest, -q]") -> None:
    repo = BENCH / "repos" / repo_name
    for num, spec in specs.items():
        task_id = f"{repo_name}-{num}"
        d = BENCH / "tasks" / task_id
        if d.exists():
            shutil.rmtree(d)
        (d / "heldout" / "tests").mkdir(parents=True)
        bug, fix = diff_for(repo, spec["edits"])
        (d / "bug.patch").write_bytes(bug.encode())
        (d / "fix.patch").write_bytes(fix.encode())
        heldout_name = f"tests/test_heldout_{spec['heldout']}.py"
        (d / "heldout" / heldout_name).write_bytes(textwrap.dedent(spec["test"]).encode())
        shutil.copyfile(repo / "tests" / "conftest.py", d / "heldout" / "tests" / "conftest.py")
        yaml = (
            f"id: {task_id}\n"
            f"repo: {repo_name}\n"
            f"title: \"{spec['title']}\"\n"
            f"issue: |\n{_yaml_block(spec['issue'])}\n"
            f"difficulty: {difficulty_for(fix)}\n"
            f"split: {spec['split']}\n"
            f"test_command: {test_command}\n"
            f"heldout_command: [pytest, -q, {heldout_name}]\n"
            f"protected_paths: {PROTECTED}\n"
            f"expected_files: [{', '.join(spec['expected_files'])}]\n"
        )
        (d / "task.yaml").write_bytes(yaml.encode())
        print(task_id, len(bug), "bytes")
