"""Open a draft pull request for an approved run.

The change is read out of the sandbox (file contents, never credentials) and rebuilt on
GitHub with the Git Data API: one blob per changed file, a tree on top of the base commit's
tree (deleted paths set to null), a commit, a branch, and a draft PR.
"""

from repopilot.agent.state import RunState
from repopilot.github.client import GitHubClient
from repopilot.sandbox_client import SandboxClient
from repopilot.tools.gitstatus import name_status_entries


def pr_body(state: RunState) -> str:
    gh = state.github
    tests = state.last_tests
    lines = [state.summary or "Automated fix proposed by RepoPilot.", ""]
    if state.plan:
        lines += ["### Diagnosis", state.plan.root_cause, ""]
        if state.plan.acceptance_criteria:
            lines += ["### Acceptance criteria"] + [f"- {c}" for c in state.plan.acceptance_criteria]
            lines.append("")
    if tests is not None:
        lines += ["### Validation", (f"`{' '.join(state.test_command)}` exited with "
                  f"{tests.exit_code} in an isolated sandbox.")]
        if state.baseline_tests_passed:
            lines.append("Note: the suite also passed before this change, so review the diff "
                         "against the issue rather than relying on green tests alone.")
        lines.append("")
    approver = state.approval.by if state.approval else "a reviewer"
    lines += [f"Fixes #{gh.issue_number}", "",
              (f"---\nOpened as a draft by RepoPilot after approval by {approver} "
              f"(run `{state.run_id}`).")]
    return "\n".join(lines)


async def _tree_entries(state: RunState, sandbox: SandboxClient, github: GitHubClient) -> list[dict]:
    sid, repo = state.sandbox_id, state.github.repo
    await sandbox.exec(sid, ["git", "add", "--intent-to-add", "--all"])
    out = (await sandbox.exec(sid, ["git", "diff", "HEAD", "--name-status", "-M", "-z"])).stdout
    entries: list[dict] = []

    def delete(path: str) -> None:
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})

    async def write(path: str) -> None:
        blob = await github.create_blob(repo, await sandbox.read_file(sid, path))
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob})

    for status, paths in name_status_entries(out):
        if status.startswith("D"):
            delete(paths[0])
        elif status.startswith("R"):
            delete(paths[0])
            await write(paths[1])
        else:  # A, M, T, and the new side of a copy
            await write(paths[-1])
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
        await self.github.create_ref(gh.repo, f"refs/heads/{branch}", commit)
        return await self.github.create_pull(gh.repo, title=title, head=branch,
                                             base=gh.base_branch, body=pr_body(state))
