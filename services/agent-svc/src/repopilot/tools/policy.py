"""What the agent may modify. Every denial is recorded as an unauthorized-modification attempt."""

import posixpath
from fnmatch import fnmatch

from repopilot.tools.patch import patch_targets

ALWAYS_PROTECTED = (".git", ".github")  # repo metadata and CI config are never the fix


def clean_rel_path(path: str) -> str | None:
    """Normalise a workspace-relative path; None when it is empty or could leave the workspace.
    Mirrors sandbox-svc's CleanRelPath so a denial here matches what the sandbox would do."""
    if not path or "\x00" in path or "\\" in path or path.startswith("/"):
        return None
    clean = posixpath.normpath(path)
    if clean in (".", "..") or clean.startswith("../"):
        return None
    return clean


class ToolPolicy:
    def __init__(self, protected_paths: list[str] | tuple[str, ...] = (), max_patch_bytes: int = 65536):
        self.protected_paths = list(protected_paths)
        self.max_patch_bytes = max_patch_bytes

    def check_write(self, path: str) -> str | None:
        """None when writing `path` is allowed, else the reason it is not."""
        clean = clean_rel_path(path)
        if clean is None:
            return f"{path!r} is not a path inside the workspace"
        parts = clean.split("/")
        if parts[0] in ALWAYS_PROTECTED:
            return f"{parts[0]}/ is protected"
        # A .git anywhere would turn a directory into a nested repo and hide it from the diff.
        if any(p.lower() == ".git" for p in parts):
            return "paths inside a .git directory are protected"
        for pattern in self.protected_paths:
            if fnmatch(clean, pattern):
                return f"{clean} is protected for this task"
        return None

    def check_patch(self, patch: str) -> str | None:
        size = len(patch.encode())
        if size > self.max_patch_bytes:
            return f"patch is {size} bytes; the limit is {self.max_patch_bytes}"
        for path in patch_targets(patch):
            if reason := self.check_write(path):
                return reason
        return None
