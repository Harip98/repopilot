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


def test_dsn_contents(monkeypatch: pytest.MonkeyPatch):
    for var in ("PG_PORT", "PG_DB", "PG_USER"):
        monkeypatch.delenv(f"REPOPILOT_{var}", raising=False)
    dsn = Settings(_env_file=None).dsn()
    assert "port=5434" in dsn
    assert "dbname=repopilot" in dsn
    assert "user=repopilot" in dsn
