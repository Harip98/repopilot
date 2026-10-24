"""The ten tools against a real sandbox holding a small committed repo."""

import json

import pytest

from repopilot.sandbox_client import SandboxError, get_sandbox_client
from repopilot.tools.builtin import build_registry
from repopilot.tools.policy import ToolPolicy
from repopilot.tools.registry import ToolContext

pytestmark = [pytest.mark.sandbox, pytest.mark.anyio]

MOD = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
TEST = "from pkg.mod import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


@pytest.fixture
async def tools(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["."]\n', newline="\n"
    )
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", newline="\n")
    (tmp_path / "pkg" / "mod.py").write_text(MOD, newline="\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mod.py").write_text(TEST, newline="\n")
    client = get_sandbox_client()
    sid = await client.create("repopilot-sandbox-base")
    await client.upload_dir(sid, tmp_path)
    for argv in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "initial"]):
        assert (await client.exec(sid, argv)).exit_code == 0
    ctx = ToolContext(
        sandbox=client,
        sandbox_id=sid,
        policy=ToolPolicy(protected_paths=["tests/test_heldout_*"]),
        test_command=["pytest", "-q"],
    )
    registry = build_registry()

    async def call(name, **args):
        return await registry.dispatch(ctx, name, json.dumps(args))

    try:
        yield call
    finally:
        try:
            await client.destroy(sid)
        except SandboxError:
            pass
        await client.aclose()


async def test_read_list_search(tools):
    out = await tools("read_file", path="pkg/mod.py", start_line=4, end_line=5)
    assert (
        out.ok
        and out.content.splitlines()[0] == "    4\t"
        and "    5\tdef sub(a, b):" in out.content
    )
    out = await tools("list_dir")
    assert out.ok and "pkg/" in out.content.splitlines() and "pkg/mod.py" in out.content
    assert ".git" not in out.content
    out = await tools("search_code", query="return a")
    assert out.ok and "pkg/mod.py:2:    return a + b" in out.content
    out = await tools("search_code", query=r"def \w+\(", regex=True)
    assert out.ok and out.content.count("pkg/mod.py") == 2
    out = await tools("search_code", query="nothing_like_this")
    assert out.ok and out.content == "no matches"


async def test_replace_text_ok_and_ambiguous(tools):
    out = await tools("replace_text", path="pkg/mod.py", old="(a, b):", new="(x, y):")
    assert not out.ok and "2 times" in out.error
    out = await tools("replace_text", path="pkg/mod.py", old="return a - b", new="return b - a")
    assert out.ok and "line 6" in out.content
    assert "return b - a" in (await tools("read_file", path="pkg/mod.py")).content


async def test_create_file_exists(tools):
    assert not (await tools("create_file", path="pkg/mod.py", content="x")).ok
    assert (await tools("create_file", path="pkg/util/new.py", content="X = 1\n")).ok
    assert "X = 1" in (await tools("read_file", path="pkg/util/new.py")).content


async def test_apply_patch_applies_and_rejects_bad_patch(tools):
    good = (
        "--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1,3 +1,3 @@\n"
        " def add(a, b):\n-    return a + b\n+    return b + a\n \n"
    )
    out = await tools("apply_patch", patch=good)
    assert out.ok, out.error
    assert "return b + a" in (await tools("read_file", path="pkg/mod.py")).content
    stale = good.replace("-    return a + b", "-    return a * b")
    out = await tools("apply_patch", patch=stale)
    assert not out.ok and "does not apply" in out.error
    denied = good.replace("pkg/mod.py", ".github/workflows/ci.yml")
    out = await tools("apply_patch", patch=denied)
    assert out.denied


async def test_run_tests_pass_and_fail_output(tools):
    out = await tools("run_tests")
    assert out.ok and out.content.startswith("exit code 0") and "1 passed" in out.content
    await tools("replace_text", path="pkg/mod.py", old="return a + b", new="return a - b")
    out = await tools("run_tests", target="tests/test_mod.py::test_add")
    assert out.ok and out.content.startswith("exit code 1") and "assert -1 == 5" in out.content


async def test_run_lint(tools):
    await tools("create_file", path="pkg/bad.py", content="import os\n")
    out = await tools("run_lint")
    assert out.ok and "F401" in out.content


async def test_git_diff_includes_new_files(tools):
    assert (await tools("git_diff")).content == "(no changes)"
    await tools("replace_text", path="pkg/mod.py", old="return a + b", new="return b + a")
    await tools("create_file", path="pkg/extra.py", content="E = 1\n")
    out = await tools("git_diff")
    assert "+    return b + a" in out.content and "+++ b/pkg/extra.py" in out.content
    assert "rp-patch" not in out.content


async def test_git_log(tools):
    out = await tools("git_log")
    assert out.ok and "initial" in out.content and "pkg/mod.py" in out.content
    out = await tools("git_log", path="pkg/mod.py", n=1)
    assert out.ok and out.content.count("initial") == 1


EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
BYPASS_PATCHES = {
    "quoted names": (
        'diff --git "a/.github/workflows/ci.yml" "b/.github/workflows/ci.yml"\n'
        "new file mode 100644\n--- /dev/null\n"
        '+++ "b/.github/workflows/ci.yml"\n@@ -0,0 +1 @@\n+on: push\n'
    ),
    "non a/b prefix": ("--- /dev/null\n+++ x/tests/test_heldout_pwn.py\n@@ -0,0 +1 @@\n+x = 1\n"),
    "header-only new file": (
        "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
        f"new file mode 100644\nindex 0000000..{EMPTY_BLOB[:7]}\n"
    ),
    "binary patch": (
        "diff --git a/.github/evil b/.github/evil\nnew file mode 100644\n"
        f"index 0000000000000000000000000000000000000000..{EMPTY_BLOB}\n"
        "GIT binary patch\nliteral 0\nHcmV?d00001\n\nliteral 0\nHcmV?d00001\n\n"
    ),
    "quoted rename": (
        'diff --git a/pkg/mod.py "b/.github/README.md"\nsimilarity index 100%\n'
        'rename from pkg/mod.py\nrename to ".github/README.md"\n'
    ),
}


@pytest.mark.parametrize("patch", BYPASS_PATCHES.values(), ids=BYPASS_PATCHES.keys())
async def test_apply_patch_bypasses_denied(tools, patch):
    out = await tools("apply_patch", patch=patch)
    assert not out.ok and out.denied, out
    diff = (await tools("git_diff")).content
    assert diff == "(no changes)", diff
