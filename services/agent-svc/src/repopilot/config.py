"""Runtime settings, read from REPOPILOT_* environment variables and the repo-root .env."""

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


REPO_ROOT = find_repo_root()


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
    llm_price_in: float = 0.0  # USD per 1M prompt tokens; 0 for local models
    llm_price_out: float = 0.0  # USD per 1M completion tokens
    github_token: SecretStr = SecretStr("")  # used host-side only; never passed into the sandbox

    def dsn(self) -> str:
        return make_conninfo(
            host=self.pg_host, port=self.pg_port, dbname=self.pg_db, user=self.pg_user,
            password=self.pg_password.get_secret_value(), connect_timeout=5,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
