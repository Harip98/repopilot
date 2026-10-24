"""Prove each benchmark task is sound: the bug applies, the held-out tests catch it, and the
reference fix makes them pass. A task that fails validation would measure nothing."""

import tempfile
from pathlib import Path

from pydantic import BaseModel

from repopilot.bench.tasks import Task, image_for
from repopilot.bench.workspace import build_base_workspace
from repopilot.sandbox_client import SandboxClient

FIX_FILE = ".git/rp-fix.diff"


class ValidationResult(BaseModel):
    task_id: str
    bug_applies: bool = False
    visible_pass_on_base: bool = False  # recorded only: a bug may legitimately break them
    heldout_fail_on_base: bool = False
    heldout_pass_with_fix: bool = False
    visible_pass_with_fix: bool = False
    detail: str = ""

    @property
    def valid(self) -> bool:
        return (
            self.bug_applies
            and self.heldout_fail_on_base
            and self.heldout_pass_with_fix
            and self.visible_pass_with_fix
        )


async def validate_task(task: Task, sandbox: SandboxClient) -> ValidationResult:
    result = ValidationResult(task_id=task.id)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            ws = build_base_workspace(task, Path(tmp) / "ws")
        except Exception as exc:  # noqa: BLE001 - any failure means the bug does not apply
            result.detail = f"bug.patch does not apply: {exc}"
            return result
        result.bug_applies = True
        sid = await sandbox.create(image_for(task))
        try:
            await sandbox.upload_dir(sid, ws)
            visible = await sandbox.exec(sid, task.test_command, timeout_s=300)
            result.visible_pass_on_base = visible.exit_code == 0
            await sandbox.upload_dir(sid, task.heldout_dir)
            heldout = await sandbox.exec(sid, task.heldout_command, timeout_s=300)
            result.heldout_fail_on_base = heldout.exit_code != 0
            await sandbox.write_file(sid, FIX_FILE, task.fix_patch.read_bytes())
            applied = await sandbox.exec(sid, ["git", "apply", FIX_FILE])
            if applied.exit_code != 0:
                result.detail = f"fix.patch does not apply: {applied.stderr.strip()}"
                return result
            heldout = await sandbox.exec(sid, task.heldout_command, timeout_s=300)
            result.heldout_pass_with_fix = heldout.exit_code == 0
            visible = await sandbox.exec(sid, task.test_command, timeout_s=300)
            result.visible_pass_with_fix = visible.exit_code == 0
            if not result.valid:
                result.detail = (heldout.stdout + visible.stdout)[-2000:]
        finally:
            await sandbox.destroy(sid)
    return result
