"""Runtime dependencies of a run, passed as LangGraph context and never checkpointed."""

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from repopilot.agent.pr import PullRequestOpener
from repopilot.agent.recorder import Recorder
from repopilot.llm.client import ChatModel
from repopilot.sandbox_client import SandboxClient
from repopilot.tools.registry import ToolRegistry


@dataclass
class RunDeps:
    llm: ChatModel
    sandbox: SandboxClient
    registry: ToolRegistry
    recorder: Recorder
    pr: PullRequestOpener
    price_in: float = 0.0  # USD per 1M prompt tokens
    price_out: float = 0.0
    clock: Callable[[], float] = field(default=time.time)
