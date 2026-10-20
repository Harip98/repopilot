import psycopg
import pytest
from psycopg import sql

from repopilot.config import Settings, get_settings
from repopilot.db.migrate import apply_migrations

TEST_DB = "repopilot_test"
TABLES = ("eval_results", "approvals", "tool_calls", "run_events", "runs")


@pytest.fixture(scope="session")
def settings() -> Settings:
    """A dedicated, migrated database so integration tests never touch dev data."""
    base = get_settings()
    try:
        with psycopg.connect(base.dsn(), autocommit=True) as conn:
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)).fetchone():
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB)))
    except psycopg.OperationalError as exc:
        pytest.fail(f"Postgres not reachable - run `docker compose up -d`: {exc}")
    s = base.model_copy(update={"pg_db": TEST_DB})
    with psycopg.connect(s.dsn(), autocommit=True) as conn:
        apply_migrations(conn)
    return s


@pytest.fixture
def conn(settings: Settings):
    with psycopg.connect(settings.dsn(), autocommit=True) as c:
        yield c
        c.execute(sql.SQL("TRUNCATE {} CASCADE").format(
            sql.SQL(", ").join(sql.Identifier(t) for t in TABLES)))
