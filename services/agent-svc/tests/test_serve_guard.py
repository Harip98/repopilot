from repopilot.api.app import bind_problem


def test_default_token_only_on_loopback():
    assert bind_problem("127.0.0.1", "dev-api-token-change-me") is None
    assert bind_problem("localhost", "dev-api-token-change-me") is None
    assert "default" in bind_problem("0.0.0.0", "dev-api-token-change-me")
    assert "short" in bind_problem("0.0.0.0", "abc")
    assert bind_problem("0.0.0.0", "a-long-random-token-0123456789abcdef") is None
