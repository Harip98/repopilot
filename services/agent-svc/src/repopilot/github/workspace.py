"""Turn a GitHub issue into a run: download the default branch, import it as a one-commit
workspace, read the repo's RepoPilot config, and start a sandbox."""

import io
import tarfile
import tempfile
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from repopilot.agent.state import Budget, GitHubTarget, RunState
from repopilot.bench.workspace import commit_all, init_repo
from repopilot.github.client import GitHubClient
from repopilot.sandbox_client import SandboxClient

CONFIG_FILE = ".repopilot.yml"


class WorkspaceError(RuntimeError):
    pass


class RepoConfig(BaseModel):
    """`.repopilot.yml` in the target repository."""

    test_command: list[str] = Field(default_factory=lambda: ["pytest", "-q"], min_length=1)
    image: str = "repopilot-sandbox-base"
    protected_paths: list[str] = Field(default_factory=list)

    @field_validator("image")
    @classmethod
    def _allowlisted(cls, v: str) -> str:
        if not v.startswith("repopilot-"):
            raise ValueError("image must be a repopilot-* sandbox image")
        return v


def load_repo_config(workspace: Path) -> RepoConfig:
    path = workspace / CONFIG_FILE
    if not path.is_file():
        return RepoConfig()
    try:
        return RepoConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    except (yaml.YAMLError, ValueError) as exc:
        raise WorkspaceError(f"invalid {CONFIG_FILE}: {exc}") from exc


def _strip_root(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo | None:
    """GitHub tarballs wrap everything in one `owner-repo-sha/` directory: drop it, then apply
    the stdlib `data` filter (no absolute or `..` paths, no links out of the tree, no devices,
    no setuid)."""
    if member.name.startswith("/"):
        raise tarfile.AbsolutePathError(member)  # never stripped into a "relative" path
    _, _, rest = member.name.partition("/")
    if not rest:
        return None
    member = member.replace(name=rest, deep=False)
    if member.islnk():
        member = member.replace(linkname=member.linkname.partition("/")[2], deep=False)
    return tarfile.data_filter(member, dest)


def extract_tarball(data: bytes, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            tar.extractall(dest, filter=_strip_root)
    except (tarfile.TarError, OSError) as exc:
        raise WorkspaceError(f"unsafe or unreadable repository archive: {exc}") from exc


def parse_issue_ref(issue_ref: str) -> tuple[str, int]:
    repo, _, number = issue_ref.rpartition("#")
    if not repo or not number.isdigit():
        raise WorkspaceError(f"bad issue reference {issue_ref!r}")
    return repo, int(number)


async def prepare_github_run(run: dict, *, run_id: str, github: GitHubClient,
                             sandbox: SandboxClient, auto_approve: bool = False,
                             budget: Budget | None = None) -> RunState:
    repo, number = parse_issue_ref(run["issue_ref"])
    issue = await github.get_issue(repo, number)
    branch, sha = await github.default_branch(repo)
    archive = await github.tarball(repo, sha)
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "ws"
        extract_tarball(archive, workspace)
        config = load_repo_config(workspace)
        init_repo(workspace)
        commit_all(workspace, f"Import {repo}@{sha[:12]}")
        sandbox_id = await sandbox.create(config.image)
        try:
            await sandbox.upload_dir(sandbox_id, workspace)
            baseline = await sandbox.exec(sandbox_id, config.test_command, timeout_s=300)
        except BaseException:
            await sandbox.destroy(sandbox_id)
            raise
    return RunState(
        run_id=run_id, repo=repo, issue_title=issue.title, issue_text=issue.body or issue.title,
        sandbox_id=sandbox_id, test_command=config.test_command,
        protected_paths=config.protected_paths, auto_approve=auto_approve,
        budget=budget or Budget(), baseline_tests_passed=baseline.exit_code == 0,
        github=GitHubTarget(repo=repo, issue_number=number, issue_url=issue.html_url,
                            base_branch=branch, base_sha=sha),
    )
