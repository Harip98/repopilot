from pathlib import Path

import pytest

from repopilot.config import REPO_ROOT, Settings, find_repo_root


def test_find_repo_root_from_subdir():
    assert find_repo_root(REPO_ROOT / "services" / "agent-svc" / "src") == REPO_ROOT


def test_find_repo_root_missing(tmp_path: Path):
    with pytest.raises(RuntimeError):
        find_repo_root(tmp_path)


def test_env_overrides(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("REPOPILOT_PG_PORT", "6000")
    assert Settings().pg_port == 6000


def test_code_index_for_worker_runs_is_opt_in(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("REPOPILOT_CODE_INDEX", raising=False)
    assert Settings(_env_file=None).code_index is False
    monkeypatch.setenv("REPOPILOT_CODE_INDEX", "true")
    assert Settings(_env_file=None).code_index is True


def test_dsn_contents(monkeypatch: pytest.MonkeyPatch):
    for var in ("PG_PORT", "PG_DB", "PG_USER"):
        monkeypatch.delenv(f"REPOPILOT_{var}", raising=False)
    dsn = Settings(_env_file=None).dsn()
    assert "port=5434" in dsn
    assert "dbname=repopilot" in dsn
    assert "user=repopilot" in dsn


def test_secrets_hidden_from_repr():
    s = Settings(
        _env_file=None,
        github_token="ghp_SECRET1",
        llm_api_key="sk-SECRET2",
        pg_password="pw-SECRET3",
        sandbox_token="sbx-SECRET4-0123456789",
    )
    text = repr(s) + str(s)
    for secret in ("ghp_SECRET1", "sk-SECRET2", "pw-SECRET3", "sbx-SECRET4-0123456789"):
        assert secret not in text
    assert "password=pw-SECRET3" in s.dsn()
    assert s.sandbox_token.get_secret_value() == "sbx-SECRET4-0123456789"


def test_repo_root_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from repopilot import config

    monkeypatch.setenv("REPOPILOT_ROOT", str(tmp_path))
    assert config.resolve_repo_root() == tmp_path.resolve()
    monkeypatch.delenv("REPOPILOT_ROOT")
    monkeypatch.setattr(config, "find_repo_root", lambda: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.chdir(tmp_path)
    assert config.resolve_repo_root() == tmp_path.resolve()
