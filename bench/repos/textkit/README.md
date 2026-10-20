# textkit

Dependency-free text helpers used by the publishing pipeline.

| Function | Behaviour |
|---|---|
| `slugify(text, max_len=None, sep="-")` | ASCII slug: accents transliterated, other characters collapsed to one separator. `max_len` never splits a word. |
| `truncate(text, limit, ellipsis="…")` | At most `limit` characters *including* the ellipsis, cut at a word boundary when possible. |
| `wrap(text, width)` | Greedy word wrap; no line is ever longer than `width` (long words are broken). |
| `parse_csv_line(line, delim=",")` | One RFC 4180 line: quoted fields, `""` is an escaped quote, empty fields kept. |
| `camel_to_snake(name)` / `snake_to_camel(name)` | `HTTPServerError` ↔ `http_server_error`, acronyms kept together. |
