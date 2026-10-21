import base64
import io
import json
import subprocess
import tarfile
from pathlib import Path

import httpx
import pytest

from repopilot.agent.state import Approval, GitHubTarget, Plan, RunState, TestReport
from repopilot.github.client import GitHubClient
from repopilot.github.pr import GitHubPR
from repopilot.github.workspace import (
    RepoConfig,
    WorkspaceError,
    extract_tarball,
    load_repo_config,
    parse_issue_ref,
    prepare_github_run,
)
from repopilot.sandbox_client import ExecResult

pytestmark = pytest.mark.anyio
TOKEN = "ghp_SECRET_never_in_sandbox"


def _ok(stdout: str = "", code: int = 0) -> ExecResult:
    return ExecResult(exit_code=code, stdout=stdout, stderr="", stdout_truncated=False,
                      stderr_truncated=False, timed_out=False, duration_ms=1)


def _tarball(members: list[tuple[tarfile.TarInfo, bytes]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for info, data in members:
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data) if data else None)
    return buf.getvalue()


def _file(name: str, data: bytes = b"x = 1\n", mode: int = 0o644) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.mode = mode
    return info, data


def _dir(name: str) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.type, info.mode = tarfile.DIRTYPE, 0o755
    return info, b""


class GitHubFake:
    """Records every request; answers the endpoints RepoPilot uses."""

    def __init__(self, archive: bytes = b""):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.archive = archive

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        path = request.url.path
        if path == "/repos/acme/shop/issues/7":
            return httpx.Response(200, json={"number": 7, "title": "Totals off by a cent",
                                             "body": "Steps...", "state": "open",
                                             "html_url": "https://github.com/acme/shop/issues/7"})
        if path == "/repos/acme/shop":
            return httpx.Response(200, json={"default_branch": "main"})
        if path == "/repos/acme/shop/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "a" * 40}})
        if path.startswith("/repos/acme/shop/tarball/"):
            return httpx.Response(200, content=self.archive)
        if path == f"/repos/acme/shop/git/commits/{'a' * 40}":
            return httpx.Response(200, json={"tree": {"sha": "basetree"}})
        if path == "/repos/acme/shop/git/blobs":
            return httpx.Response(201, json={"sha": f"blob{len(self.calls)}"})
        if path == "/repos/acme/shop/git/trees":
            return httpx.Response(201, json={"sha": "newtree"})
        if path == "/repos/acme/shop/git/commits":
            return httpx.Response(201, json={"sha": "newcommit"})
        if path == "/repos/acme/shop/git/refs":
            return httpx.Response(201, json={})
        if path == "/repos/acme/shop/pulls":
            return httpx.Response(201, json={"html_url": "https://github.com/acme/shop/pull/8"})
        return httpx.Response(404, json={"message": "Not Found"})


class SandboxSpy:
    """A sandbox that records everything it is sent, to prove the token never goes there."""

    def __init__(self, files: dict[str, bytes] | None = None, name_status: str = ""):
        self.files, self.name_status = files or {}, name_status
        self.seen: list[str] = []
        self.uploaded: list[Path] = []

    async def create(self, image: str) -> str:
        self.seen.append(image)
        return "0123456789abcdef"

    async def upload_dir(self, sandbox_id: str, src: Path) -> None:
        self.uploaded.append(src)
        self.seen += [p.read_bytes().decode(errors="ignore") for p in src.rglob("*")
                      if p.is_file() and ".git" not in p.parts]

    async def exec(self, sandbox_id, argv, timeout_s=120, cwd=None) -> ExecResult:
        self.seen.append(" ".join(argv))
        if "--name-status" in argv:
            return _ok(self.name_status)
        return _ok("1 passed")

    async def read_file(self, sandbox_id: str, path: str) -> bytes:
        self.seen.append(path)
        return self.files[path]

    async def destroy(self, sandbox_id: str) -> None:
        pass


def _client(fake: GitHubFake) -> GitHubClient:
    return GitHubClient(TOKEN, "https://api.github.test", transport=httpx.MockTransport(fake))


async def test_get_issue():
    issue = await _client(GitHubFake()).get_issue("acme/shop", 7)
    assert (issue.number, issue.title, issue.state) == (7, "Totals off by a cent", "open")


def test_tarball_extraction_is_safe(tmp_path: Path):
    root = "acme-shop-aaaaaaa/"
    good = _tarball([_dir(root), _file(root + "app/main.py"), _file(root + "README.md")])
    extract_tarball(good, tmp_path / "ok")
    assert (tmp_path / "ok" / "app" / "main.py").read_bytes() == b"x = 1\n"

    escape = tarfile.TarInfo(root + "evil")
    escape.type, escape.linkname = tarfile.SYMTYPE, "/etc/passwd"
    device = tarfile.TarInfo(root + "mem")
    device.type = tarfile.CHRTYPE
    for bad in ([_file(root + "../../outside.py")], [_file("/abs/path.py")],
                [(escape, b""), _file(root + "evil/x")], [(device, b"")]):
        with pytest.raises(WorkspaceError):
            extract_tarball(_tarball(bad), tmp_path / "bad")
    assert not (tmp_path / "outside.py").exists()


def test_setuid_bits_are_dropped(tmp_path: Path):
    root = "acme-shop-aaaaaaa/"
    extract_tarball(_tarball([_dir(root), _file(root + "run.sh", b"#!/bin/sh\n", 0o4755)]), tmp_path)
    assert (tmp_path / "run.sh").stat().st_mode & 0o4000 == 0


def test_repo_config_defaults_and_override(tmp_path: Path):
    assert load_repo_config(tmp_path) == RepoConfig()
    (tmp_path / ".repopilot.yml").write_text("test_command: [python, -m, pytest, tests]\n"
                                             "image: repopilot-bench-orders-api\n")
    config = load_repo_config(tmp_path)
    assert config.test_command == ["python", "-m", "pytest", "tests"]
    assert config.image == "repopilot-bench-orders-api"
    (tmp_path / ".repopilot.yml").write_text("image: ubuntu:latest\n")
    with pytest.raises(WorkspaceError):
        load_repo_config(tmp_path)


def test_parse_issue_ref():
    assert parse_issue_ref("acme/shop#7") == ("acme/shop", 7)
    with pytest.raises(WorkspaceError):
        parse_issue_ref("acme/shop")


async def test_workspace_import_commit_and_token_never_sent_to_sandbox():
    root = "acme-shop-aaaaaaa/"
    fake = GitHubFake(_tarball([_dir(root), _file(root + "app/main.py")]))
    sandbox = SandboxSpy()
    state = await prepare_github_run({"issue_ref": "acme/shop#7"}, run_id="run-1",
                                     github=_client(fake), sandbox=sandbox)
    assert state.issue_title == "Totals off by a cent" and state.repo == "acme/shop"
    assert state.github == GitHubTarget(repo="acme/shop", issue_number=7,
                                        issue_url="https://github.com/acme/shop/issues/7",
                                        base_branch="main", base_sha="a" * 40)
    assert state.test_command == ["pytest", "-q"] and state.baseline_tests_passed
    assert all(TOKEN not in item for item in sandbox.seen)


def _approved_state(**kw) -> RunState:
    base = {"run_id": "abcdef12-3456-7890-abcd-ef1234567890", "repo": "acme/shop",
            "issue_title": "Totals off by a cent", "issue_text": "Steps...",
            "sandbox_id": "0123456789abcdef", "test_command": ["pytest", "-q"],
            "summary": "Round once at the end.",
            "plan": Plan(root_cause="float tax", files=["app/pricing.py"],
                         acceptance_criteria=["$10.00 totals $10.83"]),
            "last_tests": TestReport(passed=True, exit_code=0),
            "approval": Approval(decision="approve", by="alice"),
            "github": GitHubTarget(repo="acme/shop", issue_number=7,
                                   issue_url="https://github.com/acme/shop/issues/7",
                                   base_branch="main", base_sha="a" * 40)}
    return RunState(**(base | kw))


async def test_open_draft_pr_calls_git_data_api_in_order():
    fake = GitHubFake()
    sandbox = SandboxSpy({"app/pricing.py": b"fixed\n"}, name_status="M\x00app/pricing.py\x00")
    url = await GitHubPR(_client(fake), sandbox).open(_approved_state())
    assert url == "https://github.com/acme/shop/pull/8"
    writes = [(m, p) for m, p, _ in fake.calls if m == "POST"]
    assert [p.rsplit("/", 1)[-1] for _, p in writes] == ["blobs", "trees", "commits", "refs", "pulls"]
    bodies = {p.rsplit("/", 1)[-1]: b for m, p, b in fake.calls if m == "POST"}
    assert base64.b64decode(bodies["blobs"]["content"]) == b"fixed\n"
    assert bodies["trees"]["base_tree"] == "basetree"
    assert bodies["commits"]["parents"] == ["a" * 40]
    assert bodies["refs"] == {"ref": "refs/heads/repopilot/abcdef12", "sha": "newcommit"}
    pull = bodies["pulls"]
    assert pull["draft"] is True and pull["base"] == "main" and pull["head"] == "repopilot/abcdef12"
    assert "Fixes #7" in pull["body"] and "alice" in pull["body"]
    assert all(TOKEN not in item for item in sandbox.seen)


async def test_pr_tree_includes_deletions():
    fake = GitHubFake()
    sandbox = SandboxSpy({"app/new.py": b"n\n"},
                         name_status="D\x00app/gone.py\x00R100\x00app/old.py\x00app/new.py\x00")
    await GitHubPR(_client(fake), sandbox).open(_approved_state())
    tree = next(b for m, p, b in fake.calls if p.endswith("/git/trees"))["tree"]
    by_path = {e["path"]: e["sha"] for e in tree}
    assert by_path["app/gone.py"] is None and by_path["app/old.py"] is None
    assert by_path["app/new.py"].startswith("blob")


async def test_benchmark_runs_open_no_pr():
    assert await GitHubPR(_client(GitHubFake()), SandboxSpy()).open(_approved_state(github=None)) is None


def test_git_available_for_import():
    assert subprocess.run(["git", "--version"], capture_output=True, check=False).returncode == 0
