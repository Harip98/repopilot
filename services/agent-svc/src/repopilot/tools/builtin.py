"""The agent's tools. Each runs inside the sandbox through sandbox-svc; none touches the host."""

import re

from pydantic import BaseModel, Field

from repopilot.sandbox_client import ExecResult, SandboxError
from repopilot.tools.policy import ToolPolicy, clean_rel_path
from repopilot.tools.registry import PolicyDenied, Tool, ToolContext, ToolError, ToolRegistry

MAX_READ_LINES = 400
MAX_SEARCH_MATCHES = 50
TEST_TIMEOUT_S = 300
PATCH_FILE = ".git/rp-patch.diff"  # inside .git: never part of the diff the agent produces
_NODE_ID = re.compile(r"^[\w./\[\]:,=-]+$")  # pytest node ids: path[::name[param]]


def _rel(path: str) -> str:
    clean = clean_rel_path(path)
    if clean is None:
        raise ToolError(f"{path!r} is not a path inside the workspace")
    return clean


def _arg_path(path: str) -> str:
    """A path safe to pass as a positional argument: never mistaken for an option."""
    clean = "." if path in ("", ".") else _rel(path)
    return clean if clean == "." else f"./{clean}"


def _exec_report(res: ExecResult, limit: int = 7000) -> str:
    """Exit status plus output, keeping the head and the tail (where test summaries are)."""
    out = res.stdout + (f"\n[stderr]\n{res.stderr}" if res.stderr.strip() else "")
    if len(out) > limit:
        head, tail = limit // 4, limit - limit // 4
        out = f"{out[:head]}\n…[{len(out) - limit} chars omitted]…\n{out[-tail:]}"
    status = f"exit code {res.exit_code}" + (" (timed out)" if res.timed_out else "")
    return f"{status}\n{out}".rstrip()


# --- read tools ---------------------------------------------------------------------------

class ReadFileArgs(BaseModel):
    path: str = Field(description="File path relative to the repository root.")
    start_line: int = Field(default=1, ge=1, description="First line to show (1-based).")
    end_line: int | None = Field(default=None, ge=1, description="Last line to show (inclusive).")


async def read_file(ctx: ToolContext, a: ReadFileArgs) -> str:
    data = await ctx.sandbox.read_file(ctx.sandbox_id, _rel(a.path))
    if b"\x00" in data[:8192]:
        raise ToolError(f"{a.path} looks like a binary file; not shown")
    lines = data.decode("utf-8", errors="replace").splitlines()
    total = len(lines)
    if total == 0:
        return f"({a.path} is empty)"
    start = min(a.start_line, total)
    end = min(a.end_line or total, total, start + MAX_READ_LINES - 1)
    body = "\n".join(f"{n:>5}\t{lines[n - 1]}" for n in range(start, end + 1))
    if start == 1 and end == total:
        return body
    return f"{body}\n[showing lines {start}-{end} of {total}]"


class ListDirArgs(BaseModel):
    path: str = Field(default=".", description="Directory relative to the repository root.")
    depth: int = Field(default=2, ge=1, le=4, description="How many levels to descend.")


async def list_dir(ctx: ToolContext, a: ListDirArgs) -> str:
    res = await ctx.sandbox.exec(ctx.sandbox_id, [
        "find", _arg_path(a.path), "-mindepth", "1", "-maxdepth", str(a.depth),
        "-not", "-path", "*/.git", "-not", "-path", "*/.git/*",
        "-not", "-path", "*/__pycache__*", "-printf", "%y %P\n",
    ])
    if res.exit_code != 0:
        raise ToolError(res.stderr.strip() or f"cannot list {a.path}")
    entries = []
    for line in sorted(res.stdout.splitlines(), key=lambda ln: ln[2:]):
        kind, _, name = line.partition(" ")
        entries.append(name + ("/" if kind == "d" else ""))
    return "\n".join(entries) or "(empty directory)"


class SearchCodeArgs(BaseModel):
    query: str = Field(min_length=1, description="Text to search for.")
    path: str = Field(default=".", description="Directory or file to search in.")
    regex: bool = Field(default=False, description="Treat query as a regular expression.")


async def search_code(ctx: ToolContext, a: SearchCodeArgs) -> str:
    argv = ["rg", "-n", "--no-heading", "--color", "never", "--hidden", "--glob", "!.git",
            "--max-columns", "300"]
    if not a.regex:
        argv.append("-F")
    path = "." if a.path in ("", ".") else _rel(a.path)
    res = await ctx.sandbox.exec(ctx.sandbox_id, [*argv, "-e", a.query, "--", path])
    if res.exit_code == 1:
        return "no matches"
    if res.exit_code != 0:
        raise ToolError(res.stderr.strip() or "search failed")
    matches = res.stdout.splitlines()
    shown = "\n".join(matches[:MAX_SEARCH_MATCHES])
    if len(matches) > MAX_SEARCH_MATCHES:
        shown += f"\n[{len(matches) - MAX_SEARCH_MATCHES} more matches; narrow the query or path]"
    return shown


class GitLogArgs(BaseModel):
    path: str | None = Field(default=None, description="Only commits touching this path.")
    n: int = Field(default=10, ge=1, le=30, description="Number of commits.")


async def git_log(ctx: ToolContext, a: GitLogArgs) -> str:
    argv = ["git", "log", f"-n{a.n}", "--stat", "--format=%h %ad %an%n    %s", "--date=short"]
    if a.path:
        argv += ["--", _rel(a.path)]
    res = await ctx.sandbox.exec(ctx.sandbox_id, argv)
    if res.exit_code != 0:
        raise ToolError(res.stderr.strip() or "git log failed")
    return res.stdout.strip() or "(no commits)"


class NoArgs(BaseModel):
    pass


async def git_diff(ctx: ToolContext, a: NoArgs) -> str:
    # Intent-to-add makes new files show up in the diff without staging their content.
    await ctx.sandbox.exec(ctx.sandbox_id, ["git", "add", "--intent-to-add", "--all"])
    res = await ctx.sandbox.exec(ctx.sandbox_id, ["git", "diff"])
    if res.exit_code != 0:
        raise ToolError(res.stderr.strip() or "git diff failed")
    return res.stdout or "(no changes)"


# --- write tools --------------------------------------------------------------------------

class ReplaceTextArgs(BaseModel):
    path: str = Field(description="File to edit.")
    old: str = Field(min_length=1, description="Exact existing text; must occur exactly once.")
    new: str = Field(description="Replacement text.")


async def replace_text(ctx: ToolContext, a: ReplaceTextArgs) -> str:
    path = _rel(a.path)
    try:
        text = (await ctx.sandbox.read_file(ctx.sandbox_id, path)).decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"{path} is not UTF-8 text; use apply_patch to change it") from None
    count = text.count(a.old)
    if count == 0:
        raise ToolError(f"old text not found in {path}; read the file again and copy it exactly")
    if count > 1:
        raise ToolError(f"old text occurs {count} times in {path}; include more surrounding "
                        "lines so it matches exactly once")
    line = text[: text.index(a.old)].count("\n") + 1
    await ctx.sandbox.write_file(ctx.sandbox_id, path, text.replace(a.old, a.new, 1).encode())
    return f"replaced 1 occurrence in {path} at line {line}"


class CreateFileArgs(BaseModel):
    path: str = Field(description="New file path relative to the repository root.")
    content: str = Field(description="Full file content.")


async def create_file(ctx: ToolContext, a: CreateFileArgs) -> str:
    path = _rel(a.path)
    try:
        await ctx.sandbox.read_file(ctx.sandbox_id, path)
    except SandboxError as exc:
        if exc.code != "not_found":
            raise ToolError(f"{path} exists and is not a regular file") from None
    else:
        raise ToolError(f"{path} already exists; use replace_text or apply_patch to change it")
    await ctx.sandbox.write_file(ctx.sandbox_id, path, a.content.encode())
    return f"created {path} ({len(a.content.splitlines())} lines)"


class ApplyPatchArgs(BaseModel):
    patch: str = Field(min_length=1, description="A unified diff (as produced by `git diff`).")


def parse_numstat_z(out: str) -> list[str]:
    """Paths from `git apply --numstat -z`: "add\\tdel\\tpath\\0", or for a rename or copy
    "add\\tdel\\t\\0old\\0new\\0"."""
    fields, paths, i = out.split("\x00"), [], 0
    while i < len(fields):
        if not fields[i]:
            i += 1
            continue
        parts = fields[i].split("\t", 2)
        if len(parts) == 3 and parts[2]:
            paths.append(parts[2])
            i += 1
        else:
            paths += [p for p in fields[i + 1:i + 3] if p]
            i += 3
    return paths


_SYMLINK_MODE = re.compile(r"^(?:new file mode|new mode|old mode|deleted file mode) 120000\s*$", re.MULTILINE)


async def apply_patch(ctx: ToolContext, a: ApplyPatchArgs) -> str:
    patch = a.patch if a.patch.endswith("\n") else a.patch + "\n"
    await ctx.sandbox.write_file(ctx.sandbox_id, PATCH_FILE, patch.encode())
    # Ask git which paths it would touch: its parser (quoting, -p1 prefixes, header-only and
    # binary patches, renames) is the one that writes files, so it is the one policy checks.
    stat = await ctx.sandbox.exec(
        ctx.sandbox_id, ["git", "apply", "--numstat", "-z", "--recount", PATCH_FILE])
    if stat.exit_code != 0:
        raise ToolError("patch does not apply: " + (stat.stderr.strip() or stat.stdout.strip()))
    targets = sorted(set(parse_numstat_z(stat.stdout)))
    if not targets:
        raise ToolError("patch contains no file changes; send a unified diff")
    for path in targets:
        if reason := ctx.policy.check_write(path):
            raise PolicyDenied(reason)
    if _SYMLINK_MODE.search(patch):
        raise PolicyDenied("patches may not create or change symlinks")
    # --recount: models often get hunk line counts wrong; the content is what must match.
    check = await ctx.sandbox.exec(
        ctx.sandbox_id, ["git", "apply", "--check", "--recount", "--whitespace=nowarn", PATCH_FILE])
    if check.exit_code != 0:
        raise ToolError("patch does not apply: " + (check.stderr.strip() or check.stdout.strip()))
    res = await ctx.sandbox.exec(
        ctx.sandbox_id, ["git", "apply", "--recount", "--whitespace=nowarn", PATCH_FILE])
    if res.exit_code != 0:
        raise ToolError("patch failed: " + res.stderr.strip())
    return "applied patch to: " + ", ".join(targets)


# --- exec tools ---------------------------------------------------------------------------

class RunTestsArgs(BaseModel):
    target: str | None = Field(
        default=None,
        description="Optional pytest target, e.g. tests/test_x.py or tests/test_x.py::test_name.",
    )


def _check_target(target: str) -> str:
    file_part = target.split("::", 1)[0]
    if target.startswith("-") or not _NODE_ID.match(target) or clean_rel_path(file_part) is None:
        raise ToolError(f"invalid test target {target!r}; use a path like tests/test_x.py::test_name")
    return target


async def run_tests(ctx: ToolContext, a: RunTestsArgs) -> str:
    argv = list(ctx.test_command) + ([_check_target(a.target)] if a.target else [])
    return _exec_report(await ctx.sandbox.exec(ctx.sandbox_id, argv, timeout_s=TEST_TIMEOUT_S))


async def run_lint(ctx: ToolContext, a: NoArgs) -> str:
    res = await ctx.sandbox.exec(ctx.sandbox_id, ["ruff", "check", "--output-format", "concise", "."])
    return _exec_report(res)


def _write_check(policy: ToolPolicy, args: ReplaceTextArgs | CreateFileArgs) -> str | None:
    return policy.check_write(args.path)


def build_registry() -> ToolRegistry:
    return ToolRegistry([
        Tool("read_file", "Read a text file with line numbers (at most 400 lines per call).",
             ReadFileArgs, read_file, "read"),
        Tool("list_dir", "List files and directories (directories end with /).",
             ListDirArgs, list_dir, "read"),
        Tool("search_code", "Search the repository for text (or a regex); returns file:line:match.",
             SearchCodeArgs, search_code, "read"),
        Tool("git_log", "Recent commits with the files they changed, optionally for one path.",
             GitLogArgs, git_log, "read"),
        Tool("git_diff", "Show all uncommitted changes, including new files.",
             NoArgs, git_diff, "read"),
        Tool("replace_text", "Replace one exact, unique occurrence of text in a file. Writes "
             "never follow symlinks.", ReplaceTextArgs, replace_text, "write", check=_write_check),
        Tool("create_file", "Create a new file. Fails if it already exists.",
             CreateFileArgs, create_file, "write", check=_write_check),
        Tool("apply_patch", "Apply a unified diff to the repository. Include 3 lines of "
             "unchanged context before and after each change, as `git diff` does.",
             ApplyPatchArgs, apply_patch, "write", check=lambda p, a: p.check_patch(a.patch)),
        Tool("run_tests", "Run the test suite, or one test file / test id.",
             RunTestsArgs, run_tests, "exec"),
        Tool("run_lint", "Run ruff on the repository.", NoArgs, run_lint, "exec"),
    ])
