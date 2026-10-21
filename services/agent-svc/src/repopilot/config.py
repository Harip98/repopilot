"""Runtime settings, read from REPOPILOT_* environment variables and the repo-root .env."""

import os
from functools import lru_cache
from pathlib import Path

from psycopg.conninfo import make_conninfo
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def find_repo_root(start: Path | None = None) -> Path:
    """Walk up from `start` to the directory holding docker-compose.yml."""
    here = (start or Path(__file__)).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "docker-compose.yml").is_file():
            return candidate
    raise RuntimeError(f"no docker-compose.yml above {here}")


def resolve_repo_root() -> Path:
    """REPOPILOT_ROOT if set (containers), else the checkout holding this package, else the
    working directory: importing settings must never fail outside a source checkout."""
    if explicit := os.environ.get("REPOPILOT_ROOT"):
        return Path(explicit).resolve()
    try:
        return find_repo_root()
    except RuntimeError:
        return Path.cwd().resolve()


REPO_ROOT = resolve_repo_root()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REPOPILOT_", env_file=REPO_ROOT / ".env", extra="ignore"
    )

    pg_host: str = "127.0.0.1"  # not "localhost": Docker binds IPv4 only
    pg_port: int = 5434  # ind-agent owns 5433
    pg_db: str = "repopilot"
    pg_user: str = "repopilot"
    pg_password: SecretStr = SecretStr("repopilot_dev_pw")
    redis_url: str = "redis://127.0.0.1:6380/0"
    sandbox_url: str = "http://127.0.0.1:8090"
    sandbox_token: SecretStr = SecretStr("dev-sandbox-token-change-me")  # matches compose default
    llm_base_url: str = "https://api.openai.com/v1"  # any OpenAI-compatible API, incl. Ollama /v1
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "gpt-4.1-mini"
    llm_timeout_s: float = 120.0
    embed_model: str = "nomic-embed-text"  # for the optional code index (768 dimensions)
    embed_base_url: str = ""  # defaults to llm_base_url
    llm_price_in: float = 0.0  # USD per 1M prompt tokens; 0 for local models
    llm_price_out: float = 0.0  # USD per 1M completion tokens
    github_token: SecretStr = SecretStr("")  # used host-side only; never passed into the sandbox
    github_api_url: str = "https://api.github.com"
    api_token: SecretStr = SecretStr("dev-api-token-change-me")  # bearer token for /v1 routes
    ui_origin: str = "http://127.0.0.1:3000"  # the only browser origin CORS allows
    job_visibility_s: int = 1800  # a job unacknowledged this long is reclaimed by another worker
    otel_endpoint: str = ""  # OTLP/HTTP base URL; empty disables trace export
    otel_capture_content: bool = False  # put prompt/completion and tool output text on spans
    langfuse_public_key: str = ""  # with the secret, sent as Basic auth to Langfuse's OTLP endpoint
    langfuse_secret_key: SecretStr = SecretStr("")

    def dsn(self) -> str:
        return make_conninfo(
            host=self.pg_host, port=self.pg_port, dbname=self.pg_db, user=self.pg_user,
            password=self.pg_password.get_secret_value(), connect_timeout=5,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
