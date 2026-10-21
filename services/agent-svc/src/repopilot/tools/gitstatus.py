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
        entries.append((status, [p for p in tokens[i + 1:i + 1 + width] if p]))
        i += 1 + width
    return entries


def changed_paths(out: str) -> list[str]:
    """Every path a change touches; both sides of a rename."""
    return [p for _, paths in name_status_entries(out) for p in paths]
