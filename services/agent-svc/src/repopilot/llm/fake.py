"""A scripted ChatModel for deterministic tests of the agent loop."""

from repopilot.llm.types import ChatResult, ToolSpec


class FakeChatModel:
    def __init__(self, script: list[ChatResult], model: str = "fake"):
        self.model = model
        self._script = list(script)
        self.calls: list[dict] = []

    async def chat(
        self, messages: list[dict], tools: list[ToolSpec] | None = None, max_tokens: int = 2048
    ) -> ChatResult:
        self.calls.append({"messages": list(messages), "tools": tools, "max_tokens": max_tokens})
        if not self._script:
            raise AssertionError(
                f"FakeChatModel script exhausted after {len(self.calls) - 1} calls"
            )
        return self._script.pop(0)
