import psycopg
import pytest

from repopilot.db.migrate import apply_migrations

pytestmark = pytest.mark.integration


def _insert_run(conn: psycopg.Connection, **overrides) -> str:
    row = {"repo": "acme/orders-api", "issue_text": "pagination skips the last page"} | overrides
    cols = ", ".join(row)
    params = ", ".join(["%s"] * len(row))
    return conn.execute(
        f"INSERT INTO runs ({cols}) VALUES ({params}) RETURNING id", list(row.values())
    ).fetchone()[0]


def _insert_eval_result(conn: psycopg.Connection, eval_run: str, task_id: str) -> None:
    conn.execute(
        "INSERT INTO eval_results (eval_run, task_id, model, resolved, patch_applied,"
        " visible_tests_passed, retries, tool_calls, tool_failures, unauthorized_attempts,"
        " latency_ms, prompt_tokens, completion_tokens, cost_usd)"
        " VALUES (%s, %s, 'fake', true, true, true, 0, 5, 0, 0, 1200, 900, 300, 0.0012)",
        (eval_run, task_id),
    )


def test_migrate_idempotent(conn: psycopg.Connection):
    assert apply_migrations(conn) == []


def test_run_defaults(conn: psycopg.Connection):
    run_id = _insert_run(conn)
    status, retries = conn.execute(
        "SELECT status, retries FROM runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert (status, retries) == ("queued", 0)


def test_run_status_check(conn: psycopg.Connection):
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_run(conn, status="bogus")


def test_cancelled_status_dropped(conn: psycopg.Connection):
    """Nothing could ever set it: there is no cancel route or command."""
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_run(conn, status="cancelled")


def test_tool_call_refused_defaults_false(conn: psycopg.Connection):
    run_id = _insert_run(conn)
    conn.execute(
        "INSERT INTO tool_calls (run_id, step, tool, args, ok, latency_ms)"
        " VALUES (%s, 1, 'read_file', '{}', true, 5)",
        (run_id,),
    )
    assert conn.execute(
        "SELECT refused FROM tool_calls WHERE run_id = %s", (run_id,)
    ).fetchone() == (False,)


def test_approval_unique_per_review_round(conn: psycopg.Connection):
    run_id = _insert_run(conn)
    insert = (
        "INSERT INTO approvals (run_id, revision, decision, decided_by)"
        " VALUES (%s, %s, 'reject', 'bob')"
    )
    conn.execute(insert, (run_id, 0))
    conn.execute(insert, (run_id, 1))
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(insert, (run_id, 0))


def test_eval_result_unique(conn: psycopg.Connection):
    _insert_eval_result(conn, "2026-10-20-dev", "orders-api-001")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_eval_result(conn, "2026-10-20-dev", "orders-api-001")


def test_run_cascade(conn: psycopg.Connection):
    run_id = _insert_run(conn)
    conn.execute(
        "INSERT INTO run_events (run_id, seq, node, kind) VALUES (%s, 1, 'intake', 'start')",
        (run_id,),
    )
    conn.execute(
        "INSERT INTO tool_calls (run_id, step, tool, args, ok, latency_ms)"
        " VALUES (%s, 1, 'read_file', '{\"path\": \"app.py\"}', true, 12)",
        (run_id,),
    )
    conn.execute("DELETE FROM runs WHERE id = %s", (run_id,))
    remaining = conn.execute(
        "SELECT (SELECT count(*) FROM run_events) + (SELECT count(*) FROM tool_calls)"
    ).fetchone()[0]
    assert remaining == 0


def test_test_db_is_fresh(conn: psycopg.Connection, session_started_at):
    """The test DB is rebuilt each session, so edits to unmerged migrations take effect."""
    applied_at = conn.execute("SELECT min(applied_at) FROM schema_migrations").fetchone()[0]
    assert applied_at >= session_started_at
