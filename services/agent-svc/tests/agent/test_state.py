from repopilot.agent.control import FINISH, SUBMIT_PLAN, tools_for
from repopilot.agent.prompts import issue_message, system_prompt
from repopilot.agent.state import Budget, Plan, RunState, RunUsage, budget_exceeded
from repopilot.tools.builtin import build_registry


def _state(**kw) -> RunState:
    base = {
        "run_id": "r1",
        "task_id": "orders-api-005",
        "repo": "orders-api",
        "issue_title": "Status filter only matches lowercase values",
        "issue_text": "GET /orders?status=PAID returns nothing.",
        "sandbox_id": "0123456789abcdef",
        "test_command": ["pytest", "-q"],
        "protected_paths": ["tests/test_heldout_*"],
        "started_at": 1000.0,
    }
    return RunState(**(base | kw))


def test_budget_exceeded_each_limit():
    b = Budget(max_llm_calls=5, max_tool_calls=9, max_tokens=100, max_seconds=60)
    assert budget_exceeded(_state(budget=b), now=1001.0) is None
    assert budget_exceeded(_state(budget=b, usage=RunUsage(llm_calls=5)), 1001.0) == "llm_calls"
    # The tools node: the reply it runs came from a call within budget.
    assert (
        budget_exceeded(_state(budget=b, usage=RunUsage(llm_calls=5)), 1001.0, llm_calls=False)
        is None
    )
    assert budget_exceeded(_state(budget=b, usage=RunUsage(tool_calls=9)), 1001.0) == "tool_calls"
    used = RunUsage(prompt_tokens=80, completion_tokens=20)
    assert budget_exceeded(_state(budget=b, usage=used), 1001.0) == "tokens"
    assert budget_exceeded(_state(budget=b), now=1060.0) == "seconds"


def test_tools_for_phases():
    reg = build_registry()
    explore = {s.name for s in tools_for(_state(), reg)}
    assert explore == {"read_file", "list_dir", "search_code", "git_log", "git_diff", "submit_plan"}
    edit = {s.name for s in tools_for(_state(phase="edit"), reg)}
    assert edit == set(reg.names) | {"finish"}
    forced = tools_for(_state(explore_steps=15), reg)
    assert [s.name for s in forced] == ["submit_plan"]
    assert SUBMIT_PLAN.parameters["required"] == ["root_cause", "files", "acceptance_criteria"]
    assert FINISH.parameters["required"] == ["summary"]


def test_system_prompt_mentions_rules_not_secrets():
    state = _state()
    prompt = system_prompt(state)
    assert "never edit tests just to make them pass" in prompt.lower()
    assert "submit_plan" in prompt and "finish" in prompt
    assert "test_heldout" not in prompt and "expected_files" not in prompt
    msg = issue_message(state)
    assert msg["role"] == "user" and "Status filter only matches lowercase values" in msg["content"]
    assert "GET /orders?status=PAID" in msg["content"]


def test_state_roundtrips_json():
    state = _state(
        phase="edit",
        plan=Plan(
            root_cause="r",
            files=["app/store.py"],
            acceptance_criteria=["a"],
            test_targets=["tests"],
        ),
        messages=[{"role": "user", "content": "hi"}],
    )
    assert RunState.model_validate_json(state.model_dump_json()) == state


def test_system_prompt_paths_relative():
    prompt = system_prompt(_state())
    assert "relative to the repository root" in prompt
    assert "Repository: orders-api" not in prompt  # read as a directory by small models
