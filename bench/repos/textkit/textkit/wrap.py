def truncate(text: str, limit: int, ellipsis: str = "…") -> str:
    """Shorten `text` to at most `limit` characters, ellipsis included, cutting at a word
    boundary when there is one."""
    if len(text) <= limit:
        return text
    room = limit - len(ellipsis)
    if room <= 0:
        return ellipsis[:limit]
    head = text[:room + 1]
    head = head.rsplit(" ", 1)[0] if " " in head else text[:room]
    return head.rstrip() + ellipsis


def wrap(text: str, width: int) -> list[str]:
    """Greedy word wrap. No line is longer than `width`: words that cannot fit on a line of
    their own are broken into `width`-sized pieces."""
    if width < 1:
        raise ValueError("width must be at least 1")
    lines: list[str] = []
    line = ""
    for word in text.split():
        while len(word) > width:
            if line:
                lines.append(line)
                line = ""
            lines.append(word[:width])
            word = word[width:]
        if not line:
            line = word
        elif len(line) + 1 + len(word) <= width:
            line = f"{line} {word}"
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines
