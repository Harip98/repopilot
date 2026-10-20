"""One client for any OpenAI-compatible chat API: hosted models and Ollama's /v1 alike."""

from typing import Protocol

import httpx
from openai import NOT_GIVEN, AsyncOpenAI

from repopilot.config import get_settings
from repopilot.llm.types import AssistantMessage, ChatResult, ToolCall, ToolSpec, Usage


class ChatModel(Protocol):
    model: str

    async def chat(
        self, messages: list[dict], tools: list[ToolSpec] | None = None, max_tokens: int = 2048
    ) -> ChatResult: ...


class OpenAICompatChat:
    def __init__(
        self, base_url: str, api_key: str, model: str, timeout_s: float, *,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.model = model
        self._client = AsyncOpenAI(
            base_url=base_url, api_key=api_key or "unused", timeout=timeout_s, max_retries=2,
            http_client=http_client,
        )

    async def chat(
        self, messages: list[dict], tools: list[ToolSpec] | None = None, max_tokens: int = 2048
    ) -> ChatResult:
        resp = await self._client.chat.completions.create(
            model=self.model, messages=messages, temperature=0, max_tokens=max_tokens,
            tools=[t.to_openai() for t in tools] if tools else NOT_GIVEN,
        )
        choice = resp.choices[0]
        calls = [
            ToolCall(id=c.id, name=c.function.name, arguments=c.function.arguments or "{}")
            for c in (choice.message.tool_calls or [])
            if c.type == "function"
        ]
        usage = Usage(
            prompt_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            completion_tokens=resp.usage.completion_tokens if resp.usage else 0,
        )
        return ChatResult(
            message=AssistantMessage(content=choice.message.content, tool_calls=calls),
            usage=usage, finish_reason=choice.finish_reason,
        )


def get_chat_model() -> OpenAICompatChat:
    s = get_settings()
    return OpenAICompatChat(s.llm_base_url, s.llm_api_key.get_secret_value(), s.llm_model, s.llm_timeout_s)
