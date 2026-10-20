import pytest

from repopilot.eval.score import EvalResult, percentile, render_markdown, summarize, wilson


def result(task_id: str, **kw) -> EvalResult:
    base = {"eval_run": "e1", "task_id": task_id, "run_id": None, "model": "m",
            "difficulty": "simple", "split": "dev", "status": "completed", "failure_reason": None,
            "resolved": True, "patch_applied": True, "visible_tests_passed": True,
            "baseline_tests_passed": True, "retries": 0, "llm_calls": 5, "tool_calls": 4,
            "tool_failures": 0, "unauthorized_attempts": 0, "test_files_modified": False,
            "latency_ms": 60_000, "prompt_tokens": 1000, "completion_tokens": 100,
            "cost_usd": 0.01, "heldout_exit_code": 0, "error": None}
    return EvalResult(**(base | kw))


def test_wilson_known_values():
    lo, hi = wilson(7, 10)
    assert lo == pytest.approx(0.397, abs=1e-3) and hi == pytest.approx(0.892, abs=1e-3)


def test_score_edges():
    assert wilson(0, 0) == (None, None)
    assert wilson(0, 5)[0] == 0.0 and wilson(5, 5)[1] == 1.0
    lo, hi = wilson(1, 1)
    assert 0.0 <= lo <= hi <= 1.0
    assert percentile([], 50) is None
    assert percentile([3.0], 95) == 3.0
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50) == 5
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10
    empty = summarize([])
    assert empty.n == 0 and empty.resolution_rate is None and empty.tokens_per_resolved is None


def test_summarize_rates():
    rows = [
        result("a-001"),
        result("a-002", resolved=False, status="failed", failure_reason="tests_failing",
               visible_tests_passed=False, retries=3, tool_calls=10, tool_failures=4,
               latency_ms=120_000, difficulty="complex"),
        result("a-003", resolved=False, patch_applied=False, visible_tests_passed=False,
               status="failed", failure_reason="budget:llm_calls", unauthorized_attempts=2,
               test_files_modified=True, latency_ms=30_000),
        result("a-004", status="error", resolved=False, patch_applied=False,
               visible_tests_passed=False, error="APIConnectionError", llm_calls=0, tool_calls=0,
               prompt_tokens=0, completion_tokens=0, cost_usd=0.0, latency_ms=1_000),
    ]
    s = summarize(rows)
    assert (s.n, s.resolved, s.errors) == (4, 1, 1)
    assert s.resolution_rate == 0.25 and s.patch_applied_rate == 0.5
    assert s.visible_pass_rate == 0.25 and s.baseline_pass_rate == 1.0
    assert s.tool_failure_rate == pytest.approx(4 / 18)
    assert s.avg_retries == 0.75
    assert s.p50_latency_s == 30.0 and s.p95_latency_s == 120.0
    assert s.tokens_per_resolved == 3300 and s.cost_per_resolved == pytest.approx(0.03)
    assert s.unauthorized_attempts == 2 and s.runs_with_test_edits == 1
    assert s.failure_reasons == {"tests_failing": 1, "budget:llm_calls": 1, "error": 1}
    assert s.by_difficulty["complex"].n == 1 and s.by_difficulty["simple"].resolved == 1


def test_render_markdown_has_table():
    rows = [result("a-001"), result("a-002", resolved=False, status="failed",
                                    failure_reason="tests_failing")]
    md = render_markdown("e1", {"model": "qwen", "git_sha": "abc123"}, summarize(rows), rows)
    assert "| Resolution rate | 50.0% (1/2) |" in md
    assert "qwen" in md and "a-002" in md and "tests_failing" in md
    assert "## Definitions" in md
