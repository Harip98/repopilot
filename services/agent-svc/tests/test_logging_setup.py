import logging

import httpx2
import pytest

from repopilot.cli import configure_logging
from repopilot.llm import OpenAICompatChat

COMPLETION = {
    "id": "c",
    "object": "chat.completion",
    "created": 0,
    "model": "m",
    "choices": [
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hi"}}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


def test_http_client_urls_are_not_logged_at_info():
    # A private repo's tarball redirect carries a short-lived token in its query string.
    configure_logging()
    # httpx/httpcore serve the GitHub and sandbox clients; the openai SDK (and mcp, langsmith)
    # send through httpx2/httpcore2, which log under their own names.
    for name in ("httpx", "httpcore", "httpx2", "httpcore2"):
        assert logging.getLogger(name).level >= logging.WARNING, name
    assert logging.getLogger("repopilot").getEffectiveLevel() <= logging.INFO


@pytest.mark.anyio
async def test_llm_request_lines_are_not_logged_at_info(caplog):
    configure_logging()
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        return httpx2.Response(200, json=COMPLETION)

    # The SDK's default client is an httpx2.AsyncClient; give it a transport that never leaves
    # the process.
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    model = OpenAICompatChat("http://llm.test/v1", "sk", "m", 5.0, http_client=http)
    with caplog.at_level(logging.INFO):
        await model.chat([{"role": "user", "content": "hi"}])
    assert seen == ["http://llm.test/v1/chat/completions"]
    assert not [
        r
        for r in caplog.records
        if r.levelno < logging.WARNING and not r.name.startswith("repopilot")
    ], caplog.records
