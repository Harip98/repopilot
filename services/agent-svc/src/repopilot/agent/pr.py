"""Opening a pull request after approval. Benchmark runs have nowhere to push."""

from typing import Protocol

from repopilot.agent.state import RunState


class PullRequestOpener(Protocol):
    async def open(self, state: RunState) -> str | None:
        """Open a draft PR for the approved change; returns its URL, or None if not applicable."""


class PRError(RuntimeError):
    """The approved change cannot be turned into a pull request automatically."""


class NoPR:
    async def open(self, state: RunState) -> str | None:
        return None
