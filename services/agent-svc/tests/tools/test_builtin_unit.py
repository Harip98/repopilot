import json

import pytest

from repopilot.sandbox_client import ExecResult, SandboxError
from repopilot.tools.builtin import build_registry
from repopilot.tools.policy import ToolPolicy
from repopilot.tools.registry import ToolContext

pytestmark = pytest.mark.anyio


class FakeSandbox:
    """In-memory files; exec returns a scripted result and records argv."""

    def __init__(self, files: dict[str, bytes] | None = None):
        self.files = dict(files or {})
        self.execs: list[list[str]] = []
        self.exec_result = ExecResult(
            exit_code=0,
            stdout="",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
            duration_ms=1,
        )

    async def read_file(self, sandbox_id: str, path: str) -> bytes:
        if path not in self.files:
            raise SandboxError("not_found", f"{path!r}", 404)
        return self.files[path]

    async def write_file(self, sandbox_id: str, path: str, data: bytes) -> None:
        self.files[path] = data

    numstat = ""  # what `git apply --numstat -z` reports

    async def exec(self, sandbox_id, argv, timeout_s=120, cwd=None) -> ExecResult:
        self.execs.append(argv)
        if "--numstat" in argv:
            return self.exec_result.model_copy(update={"stdout": self.numstat})
        return self.exec_result


def _ctx(sb: FakeSandbox, protected: list[str] | None = None) -> ToolContext:
    return ToolContext(
        sandbox=sb,
        sandbox_id="0123456789abcdef",
        policy=ToolPolicy(protected_paths=protected or []),
        test_command=["pytest", "-q"],
    )


async def _call(sb, name, protected=None, **args):
    return await build_registry().dispatch(_ctx(sb, protected), name, json.dumps(args))


def test_registry_has_the_ten_tools():
    reg = build_registry()
    assert sorted(reg.names) == sorted(
        [
            "read_file",
            "list_dir",
            "search_code",
            "replace_text",
            "create_file",
            "apply_patch",
            "run_tests",
            "run_lint",
            "git_diff",
            "git_log",
        ]
    )
    assert sorted(s.name for s in reg.subset({"read"}).specs()) == [
        "git_diff",
        "git_log",
        "list_dir",
        "read_file",
        "search_code",
    ]


async def test_read_file_numbers_and_caps():
    body = "".join(f"line {i}\n" for i in range(1, 1001)).encode()
    sb = FakeSandbox({"app/x.py": body})
    out = await _call(sb, "read_file", path="app/x.py")
    lines = out.content.splitlines()
    assert lines[0] == "    1\tline 1" and lines[399] == "  400\tline 400"
    assert "lines 1-400 of 1000" in out.content
    out = await _call(sb, "read_file", path="app/x.py", start_line=998)
    assert out.content.splitlines()[0] == "  998\tline 998" and "line 1000" in out.content


async def test_read_file_binary_refused():
    out = await _call(FakeSandbox({"img.png": b"\x89PNG\x00\x00"}), "read_file", path="img.png")
    assert not out.ok and "binary" in out.error


async def test_replace_text_counts_matches():
    sb = FakeSandbox({"a.py": b"x = 1\nx = 1\ny = 2\n"})
    out = await _call(sb, "replace_text", path="a.py", old="x = 1", new="x = 3")
    assert not out.ok and "2 times" in out.error and sb.files["a.py"] == b"x = 1\nx = 1\ny = 2\n"
    out = await _call(sb, "replace_text", path="a.py", old="z = 9", new="z = 0")
    assert not out.ok and "not found" in out.error
    out = await _call(sb, "replace_text", path="a.py", old="y = 2", new="y = 5")
    assert out.ok and sb.files["a.py"] == b"x = 1\nx = 1\ny = 5\n" and "line 3" in out.content


async def test_create_file_refuses_existing():
    sb = FakeSandbox({"a.py": b"old"})
    out = await _call(sb, "create_file", path="a.py", content="new")
    assert not out.ok and "exists" in out.error and sb.files["a.py"] == b"old"
    out = await _call(sb, "create_file", path="pkg/b.py", content="B = 1\n")
    assert out.ok and sb.files["pkg/b.py"] == b"B = 1\n"


async def test_apply_patch_denied_paths():
    sb = FakeSandbox()
    for target in (".github/workflows/ci.yml", "tests/heldout_x.py", ".git/config"):
        patch = f"--- a/{target}\n+++ b/{target}\n@@ -1 +1 @@\n-a\n+b\n"
        out = await _call(sb, "apply_patch", protected=["tests/heldout_*"], patch=patch)
        assert not out.ok and out.denied, target
    assert sb.files == {} and sb.execs == []


async def test_apply_patch_runs_check_then_apply():
    sb = FakeSandbox()
    sb.numstat = "1\t1\tapp/x.py\x00"
    patch = "--- a/app/x.py\n+++ b/app/x.py\n@@ -1 +1 @@\n-a\n+b"
    out = await _call(sb, "apply_patch", patch=patch)
    assert out.ok and "app/x.py" in out.content
    assert sb.files[".git/rp-patch.diff"].endswith(b"+b\n")
    assert [a[2] for a in sb.execs] == ["--numstat", "--check", "--recount"]


async def test_run_tests_rejects_bad_target():
    sb = FakeSandbox()
    for target in ("../x.py", "--rootdir=/", "-k foo", "tests/a.py b.py", "/etc/x"):
        out = await _call(sb, "run_tests", target=target)
        assert not out.ok, target
    assert sb.execs == []
    out = await _call(sb, "run_tests", target="tests/test_orders.py::test_page[2-10]")
    assert out.ok and sb.execs[-1] == ["pytest", "-q", "tests/test_orders.py::test_page[2-10]"]


async def test_list_dir_and_search_guard_option_injection():
    sb = FakeSandbox()
    await _call(sb, "list_dir", path="-delete")
    assert "./-delete" in sb.execs[-1] and "-delete" not in sb.execs[-1][:2]
    await _call(sb, "search_code", query="--files", path="app")
    argv = sb.execs[-1]
    assert argv[argv.index("-e") + 1] == "--files" and argv[-2:] == ["--", "app"]


def _applies(sb) -> bool:
    return any(
        argv[:2] == ["git", "apply"] and "--numstat" not in argv and "--check" not in argv
        for argv in sb.execs
    )


async def test_apply_patch_policy_uses_git_paths():
    """Headers git reads differently from a naive parser (quoted, x/ prefix, header-only,\n    binary): the paths git reports are what policy checks."""
    sb = FakeSandbox()
    sb.numstat = "1\t0\t.github/workflows/ci.yml\x00"
    patch = '--- "a/x"\n+++ "b/.github/workflows/ci.yml"\n@@ -0,0 +1 @@\n+on: push\n'
    out = await _call(sb, "apply_patch", patch=patch)
    assert not out.ok and out.denied and ".github" in out.error
    assert not _applies(sb)


async def test_apply_patch_rename_into_protected_path():
    sb = FakeSandbox()
    sb.numstat = "0\t0\t\x00app/a.py\x00tests/test_heldout_x.py\x00"
    out = await _call(
        sb,
        "apply_patch",
        protected=["tests/test_heldout_*"],
        patch="diff --git a/app/a.py b/tests/test_heldout_x.py\nrename from app/a.py\n",
    )
    assert out.denied and "test_heldout_x.py" in out.error and not _applies(sb)


async def test_apply_patch_refuses_symlinks():
    sb = FakeSandbox()
    sb.numstat = "1\t0\tlnk\x00"
    patch = "diff --git a/lnk b/lnk\nnew file mode 120000\n--- /dev/null\n+++ b/lnk\n@@ -0,0 +1 @@\n+/etc\n"
    out = await _call(sb, "apply_patch", patch=patch)
    assert out.denied and "symlink" in out.error and not _applies(sb)


async def test_apply_patch_without_changes():
    sb = FakeSandbox()
    out = await _call(sb, "apply_patch", patch="just some text\n")
    assert not out.ok and not out.denied and "no file changes" in out.error


async def test_replace_text_non_utf8():
    out = await _call(
        FakeSandbox({"a.txt": "caf\xe9".encode("latin-1")}),
        "replace_text",
        path="a.txt",
        old="caf",
        new="tea",
    )
    assert not out.ok and "UTF-8" in out.error
