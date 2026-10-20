import pytest

from repopilot.agent.runner import BenchOutcome, GradeResult, RunResult
from repopilot.agent.state import RunState, RunUsage, TestReport
from repopilot.bench.tasks import load_tasks
from repopilot.eval.runner import MemoryResultStore, edited_existing_tests, run_eval

pytestmark = pytest.mark.anyio

TASKS = [t for t in load_tasks() if t.id in ("orders-api-001", "orders-api-002", "orders-api-008")]
DIFF = ("diff --git a/app/store.py b/app/store.py\nindex 1..2 100644\n--- a/app/store.py\n"
        "+++ b/app/store.py\n@@ -1 +1 @@\n-a\n+b\n")


def outcome(task_id: str, *, resolved=True, status="completed", diff=DIFF, **state_kw) -> BenchOutcome:
    state = RunState(
        run_id="r", task_id=task_id, repo="orders-api", issue_title="t", issue_text="i",
        sandbox_id="s", test_command=["pytest"], status=status, diff=diff, retries=1,
        baseline_tests_passed=True,
        last_tests=TestReport(passed=status == "completed", exit_code=0),
        usage=RunUsage(llm_calls=6, tool_calls=5, tool_failures=1, unauthorized_attempts=0,
                       prompt_tokens=900, completion_tokens=90, cost_usd=0.002),
        **state_kw,
    )
    graded = GradeResult(resolved=resolved, exit_code=0 if resolved else 1, output_tail="")
    return BenchOutcome(result=RunResult(state=state, interrupt=None),
                        graded=graded if status == "completed" else None)


class FakeRunTask:
    def __init__(self, outcomes: dict):
        self.outcomes, self.calls = outcomes, []

    async def __call__(self, task, run_id):
        self.calls.append(task.id)
        value = self.outcomes[task.id]
        if isinstance(value, Exception):
            raise value
        return value


async def test_result_fields_from_state():
    store = MemoryResultStore()
    run_task = FakeRunTask({t.id: outcome(t.id) for t in TASKS})
    results = await run_eval("e1", TASKS[:1], run_task, store, model="qwen")
    [r] = results
    assert (r.eval_run, r.task_id, r.model, r.status, r.resolved) == ("e1", "orders-api-001", "qwen",
                                                                       "completed", True)
    assert (r.llm_calls, r.tool_calls, r.tool_failures, r.retries) == (6, 5, 1, 1)
    assert (r.prompt_tokens, r.completion_tokens, r.cost_usd) == (900, 90, 0.002)
    assert r.patch_applied and r.visible_tests_passed and r.baseline_tests_passed
    assert r.heldout_exit_code == 0 and r.split == "dev" and r.difficulty == "simple"
    assert r.latency_ms >= 0


async def test_eval_resumes():
    store = MemoryResultStore()
    first = FakeRunTask({t.id: outcome(t.id) for t in TASKS})
    await run_eval("e1", TASKS[:2], first, store, model="m")
    second = FakeRunTask({t.id: outcome(t.id) for t in TASKS})
    results = await run_eval("e1", TASKS, second, store, model="m")
    assert second.calls == ["orders-api-008"]
    assert sorted(r.task_id for r in results) == [t.id for t in TASKS]


async def test_task_error_recorded_and_continues():
    store = MemoryResultStore()
    run_task = FakeRunTask({"orders-api-001": ConnectionError("ollama is down"),
                            "orders-api-002": outcome("orders-api-002")})
    results = await run_eval("e1", TASKS[:2], run_task, store, model="m")
    by_id = {r.task_id: r for r in results}
    err = by_id["orders-api-001"]
    assert err.status == "error" and not err.resolved and "ollama is down" in err.error
    assert by_id["orders-api-002"].resolved


async def test_unfinished_run_is_not_resolved():
    store = MemoryResultStore()
    run_task = FakeRunTask({"orders-api-001": outcome("orders-api-001", status="failed",
                                                      failure_reason="budget:llm_calls")})
    [r] = await run_eval("e1", TASKS[:1], run_task, store, model="m")
    assert not r.resolved and r.failure_reason == "budget:llm_calls" and r.heldout_exit_code is None


def test_test_files_modified_flag():
    edited = DIFF + ("diff --git a/tests/test_orders_api.py b/tests/test_orders_api.py\n"
                     "index 1..2 100644\n--- a/tests/test_orders_api.py\n"
                     "+++ b/tests/test_orders_api.py\n@@ -1 +1 @@\n-x\n+y\n")
    added = DIFF + ("diff --git a/tests/test_new.py b/tests/test_new.py\nnew file mode 100644\n"
                    "--- /dev/null\n+++ b/tests/test_new.py\n@@ -0,0 +1 @@\n+z\n")
    deleted = ("diff --git a/tests/test_pricing.py b/tests/test_pricing.py\n"
               "deleted file mode 100644\n--- a/tests/test_pricing.py\n+++ /dev/null\n")
    assert edited_existing_tests(edited) == ["tests/test_orders_api.py"]
    assert edited_existing_tests(added) == []
    assert edited_existing_tests(deleted) == ["tests/test_pricing.py"]
