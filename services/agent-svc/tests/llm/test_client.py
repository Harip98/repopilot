import json

import httpx
import pytest

from repopilot.llm import (
    AssistantMessage,
    ChatResult,
    FakeChatModel,
    OpenAICompatChat,
    ToolCall,
    ToolSpec,
    Usage,
    cost_usd,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


READ_FILE = ToolSpec(
    name="read_file", description="Read a file.",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
)


def _completion(message: dict, usage: dict | None) -> dict:
    body = {
        "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": message}],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def _model(handler) -> OpenAICompatChat:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatChat("http://llm/v1", "sk-test", "test-model", 30.0, http_client=http)


async def test_openai_compat_parses_tool_calls():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion(
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "read_file", "arguments": "{\"path\": \"a.py\"}"},
            }]},
            {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
        ))

    result = await _model(handler).chat([{"role": "user", "content": "hi"}], tools=[READ_FILE])
    assert result.message == AssistantMessage(
        content=None, tool_calls=[ToolCall(id="call_1", name="read_file", arguments='{"path": "a.py"}')]
    )
    assert result.usage == Usage(prompt_tokens=120, completion_tokens=30)
    assert result.finish_reason == "tool_calls"
    assert seen["path"] == "/v1/chat/completions"
    body = seen["body"]
    assert body["model"] == "test-model" and body["temperature"] == 0
    assert body["tools"] == [{"type": "function", "function": {
        "name": "read_file", "description": "Read a file.", "parameters": READ_FILE.parameters,
    }}]


async def test_openai_compat_missing_usage():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion({"role": "assistant", "content": "done"}, None))

    result = await _model(handler).chat([{"role": "user", "content": "hi"}])
    assert result.message.content == "done" and result.message.tool_calls == []
    assert result.usage == Usage(prompt_tokens=0, completion_tokens=0)


def test_assistant_message_to_dict_roundtrips_tool_calls():
    msg = AssistantMessage(content=None, tool_calls=[ToolCall(id="c1", name="git_diff", arguments="{}")])
    assert msg.to_dict() == {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "git_diff", "arguments": "{}"}},
    ]}
    assert AssistantMessage(content="ok").to_dict() == {"role": "assistant", "content": "ok"}


async def test_fake_replays_and_exhausts():
    first = ChatResult(message=AssistantMessage(content="one"), usage=Usage(prompt_tokens=1, completion_tokens=1))
    second = ChatResult(message=AssistantMessage(content="two"), usage=Usage(prompt_tokens=2, completion_tokens=2))
    fake = FakeChatModel([first, second])
    assert (await fake.chat([{"role": "user", "content": "a"}])).message.content == "one"
    assert (await fake.chat([{"role": "user", "content": "b"}], tools=[READ_FILE])).message.content == "two"
    assert [c["messages"][-1]["content"] for c in fake.calls] == ["a", "b"]
    assert fake.calls[1]["tools"] == [READ_FILE]
    with pytest.raises(AssertionError, match="script exhausted"):
        await fake.chat([{"role": "user", "content": "c"}])


def test_cost_usd():
    assert cost_usd(Usage(prompt_tokens=1000, completion_tokens=500), 0.4, 1.6) == pytest.approx(0.0012)
    assert cost_usd(Usage(prompt_tokens=1000, completion_tokens=500), 0.0, 0.0) == 0.0
