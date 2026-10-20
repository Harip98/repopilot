import re

# A boundary before an upper-case letter that follows a lower-case letter or digit
# (userName), and inside a run of capitals before the last one (HTTPServer -> HTTP|Server).
_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def camel_to_snake(name: str) -> str:
    """userName -> user_name, HTTPServerError -> http_server_error, userID -> user_id."""
    return _BOUNDARY.sub("_", name).lower()


def snake_to_camel(name: str, upper_first: bool = False) -> str:
    """user_name -> userName (or UserName). Leading underscores are kept: _private_value ->
    _privateValue."""
    stripped = name.lstrip("_")
    prefix = name[: len(name) - len(stripped)]
    parts = [p for p in stripped.split("_") if p]
    if not parts:
        return name
    head = parts[0].capitalize() if upper_first else parts[0]
    return prefix + head + "".join(p.capitalize() for p in parts[1:])
