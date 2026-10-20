"""Find every file a unified diff would touch, so policy can vet it before `git apply`."""

import re

_HEADER = re.compile(r"^(?:---|\+\+\+) (.+)$")
_MOVE = re.compile(r"^(?:rename|copy) (?:from|to) (.+)$")


def _strip(raw: str) -> str | None:
    path = raw.split("\t", 1)[0].strip()
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def patch_targets(diff: str) -> list[str]:
    targets: set[str] = set()
    for line in diff.splitlines():
        m = _HEADER.match(line) or _MOVE.match(line)
        if m and (path := _strip(m.group(1))):
            targets.add(path)
    return sorted(targets)
