def parse_csv_line(line: str, delim: str = ",") -> list[str]:
    """Split one CSV line (RFC 4180). Fields may be quoted; inside quotes a doubled quote is a
    literal quote and the delimiter is ordinary text. Empty fields are kept."""
    fields: list[str] = []
    buf: list[str] = []
    quoted = False
    i = 0
    while i < len(line):
        ch = line[i]
        if quoted:
            if ch == '"':
                if i + 1 < len(line) and line[i + 1] == '"':
                    buf.append('"')
                    i += 1
                else:
                    quoted = False
            else:
                buf.append(ch)
        elif ch == '"':
            quoted = True
        elif ch == delim:
            fields.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    if quoted:
        raise ValueError("unterminated quoted field")
    fields.append("".join(buf))
    return fields
