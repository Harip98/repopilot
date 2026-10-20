from datetime import UTC, datetime

import psycopg
import pytest
from psycopg import sql

from repopilot.config import Settings, get_settings
from repopilot.db.migrate import apply_migrations

TEST_DB = "repopilot_test"
_SESSION_STARTED_AT = datetime.now(UTC)


@pytest.fixture(scope="session")
def settings() -> Settings:
    """A dedicated database, rebuilt and migrated each session so integration tests never touch
    dev data and always run against the migration files as they are now."""
    base = get_settings()
    try:
        with psycopg.connect(base.dsn(), autocommit=True) as conn:
            db = sql.Identifier(TEST_DB)
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(db))
            conn.execute(sql.SQL("CREATE DATABASE {}").format(db))
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
        tables = [r[0] for r in c.execute(
            "SELECT tablename FROM pg_tables"
            " WHERE schemaname = 'public' AND tablename <> 'schema_migrations'").fetchall()]
        c.execute(sql.SQL("TRUNCATE {} CASCADE").format(
            sql.SQL(", ").join(sql.Identifier(t) for t in tables)))


@pytest.fixture(scope="session")
def session_started_at(settings: Settings):
    return _SESSION_STARTED_AT
