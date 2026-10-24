"""Open a draft pull request for an approved run.

The change is read out of the sandbox (file contents, never credentials) and rebuilt on
GitHub with the Git Data API: one blob per changed file, a tree on top of the base commit's
tree (deleted paths set to null), a commit, a branch, and a draft PR. File modes are kept
(an executable stays executable); symlinks and submodules are refused rather than guessed.
Opening is idempotent: a retry after a partial failure moves the existing branch and finds
the existing pull request.
"""

from repopilot.agent.pr import PRError
from repopilot.agent.state import RunState
from repopilot.github.client import GitHubClient, GitHubError
from repopilot.sandbox_client import SandboxClient
from repopilot.tools.gitstatus import raw_entries

_FILE_MODES = {"100644", "100755"}

__all__ = ["GitHubPR", "PRError", "pr_body"]


def pr_body(state: RunState) -> str:
    gh = state.github
    tests = state.last_tests
    lines = [state.summary or "Automated fix proposed by RepoPilot.", ""]
    if state.plan:
        lines += ["### Diagnosis", state.plan.root_cause, ""]
        if state.plan.acceptance_criteria:
            lines += ["### Acceptance criteria"] + [
                f"- {c}" for c in state.plan.acceptance_criteria
            ]
            lines.append("")
    if tests is not None:
        lines += [
            "### Validation",
            (
                f"`{' '.join(state.test_command)}` exited with "
                f"{tests.exit_code} in an isolated sandbox."
            ),
        ]
        if state.baseline_tests_passed:
            lines.append(
                "Note: the suite also passed before this change, so review the diff "
                "against the issue rather than relying on green tests alone."
            )
        lines.append("")
    approver = state.approval.by if state.approval else "a reviewer"
    lines += [
        f"Fixes #{gh.issue_number}",
        "",
        (
            f"---\nOpened as a draft by RepoPilot after approval by {approver} "
            f"(run `{state.run_id}`)."
        ),
    ]
    return "\n".join(lines)


async def _tree_entries(
    state: RunState, sandbox: SandboxClient, github: GitHubClient
) -> list[dict]:
    sid, repo = state.sandbox_id, state.github.repo
    await sandbox.exec(sid, ["git", "add", "--intent-to-add", "--all"])
    out = (await sandbox.exec(sid, ["git", "diff", "HEAD", "--raw", "-M", "-z"])).stdout
    changes = raw_entries(out)
    odd = [
        paths[-1]
        for status, mode, paths in changes
        if not status.startswith("D") and mode not in _FILE_MODES
    ]
    if odd:
        raise PRError(
            f"the change adds symlinks or submodules ({', '.join(odd)}); "
            "open this pull request by hand"
        )
    entries: list[dict] = []

    def delete(path: str) -> None:
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})

    async def write(path: str, mode: str) -> None:
        blob = await github.create_blob(repo, await sandbox.read_file(sid, path))
        entries.append({"path": path, "mode": mode, "type": "blob", "sha": blob})

    for status, mode, paths in changes:
        if status.startswith("D"):
            delete(paths[0])
        elif status.startswith("R"):
            delete(paths[0])
            await write(paths[1], mode)
        else:  # A, M, T, and the new side of a copy
            await write(paths[-1], mode)
    return entries


class GitHubPR:
    def __init__(self, github: GitHubClient, sandbox: SandboxClient):
        self.github, self.sandbox = github, sandbox

    async def open(self, state: RunState) -> str | None:
        gh = state.github
        if gh is None:
            return None  # benchmark runs have nowhere to push
        entries = await _tree_entries(state, self.sandbox, self.github)
        if not entries:
            return None
        title = f"RepoPilot: {state.issue_title} (#{gh.issue_number})"
        base_tree = await self.github.commit_tree(gh.repo, gh.base_sha)
        tree = await self.github.create_tree(gh.repo, base_tree, entries)
        commit = await self.github.create_commit(gh.repo, title, tree, [gh.base_sha])
        branch = f"repopilot/{state.run_id[:8]}"
        try:
            await self.github.create_ref(gh.repo, f"refs/heads/{branch}", commit)
        except GitHubError as exc:
            if exc.status != 422:
                raise
            # Left by an earlier attempt of this run: the branch is ours, point it at the commit.
            await self.github.update_ref(gh.repo, f"heads/{branch}", commit)
        try:
            return await self.github.create_pull(
                gh.repo, title=title, head=branch, base=gh.base_branch, body=pr_body(state)
            )
        except GitHubError as exc:
            if exc.status != 422 or not (url := await self.github.find_pull(gh.repo, branch)):
                raise
            return url
