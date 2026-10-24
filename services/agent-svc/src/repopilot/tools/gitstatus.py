"""Parse `git diff --name-status -z` output, which is unambiguous for any file name."""


def name_status_entries(out: str) -> list[tuple[str, list[str]]]:
    """[(status, paths)]: one path, or (old, new) for renames (R) and copies (C)."""
    tokens, entries, i = out.split("\x00"), [], 0
    while i < len(tokens):
        status = tokens[i]
        if not status:
            i += 1
            continue
        width = 2 if status[0] in "RC" else 1
        entries.append((status, [p for p in tokens[i + 1 : i + 1 + width] if p]))
        i += 1 + width
    return entries


def changed_paths(out: str) -> list[str]:
    """Every path a change touches; both sides of a rename."""
    return [p for _, paths in name_status_entries(out) for p in paths]


def raw_entries(out: str) -> list[tuple[str, str, list[str]]]:
    """[(status, new mode, paths)] from `git diff --raw -z`: like name_status_entries, plus the
    mode the file has now (100755 for executables, 120000 for symlinks, 160000 submodules)."""
    tokens, entries, i = out.split("\x00"), [], 0
    while i < len(tokens):
        header = tokens[i]
        if not header.startswith(":"):
            i += 1
            continue
        _, new_mode, _, _, status = header[1:].split(" ")
        width = 2 if status[0] in "RC" else 1
        entries.append((status, new_mode, [p for p in tokens[i + 1 : i + 1 + width] if p]))
        i += 1 + width
    return entries
