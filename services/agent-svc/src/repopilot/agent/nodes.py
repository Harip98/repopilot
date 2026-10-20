"""The agent graph's nodes. Each returns a partial state update and sets `route`, which the
graph's conditional edges follow. One node execution is one checkpointed step."""

from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import ValidationError

from repopilot.agent.control import CONTROL_TOOLS, allowed_tool_names, tools_for
from repopilot.agent.deps import RunDeps
from repopilot.agent.prompts import issue_message, system_prompt
from repopilot.agent.state import Approval, Plan, RunState, TestReport, budget_exceeded
from repopilot.llm.types import ToolCall, cost_usd
from repopilot.tools.policy import ToolPolicy
from repopilot.tools.registry import ToolContext

MAX_NUDGES = 2
TEST_TIMEOUT_S = 300
OUTPUT_TAIL = 4000
NUDGE = ("Reply with a tool call. Explore with the read tools, call submit_plan once you know "
         "the cause, and call finish when your fix is complete.")


def _user(content: str) -> dict:
    return {"role": "user", "content": content}


async def intake(state: RunState, runtime: Runtime[RunDeps]) -> dict:
    deps = runtime.context
    await deps.recorder.status(state.run_id, "running")
    return {
        "messages": [{"role": "system", "content": system_prompt(state)}, issue_message(state)],
        "started_at": state.started_at or deps.clock(),
        "route": "llm",
    }


async def llm(state: RunState, runtime: Runtime[RunDeps]) -> dict:
    deps = runtime.context
    if reason := budget_exceeded(state, deps.clock()):
        return {"failure_reason": f"budget:{reason}", "route": "fail"}
    result = await deps.llm.chat(state.messages, tools_for(state, deps.registry))
    reply = result.message
    message = reply.to_dict()
    if message["content"] is None and not reply.tool_calls:
        message["content"] = ""  # OpenAI rejects an assistant turn with neither
    u = state.usage
    usage = u.model_copy(update={
        "llm_calls": u.llm_calls + 1,
        "prompt_tokens": u.prompt_tokens + result.usage.prompt_tokens,
        "completion_tokens": u.completion_tokens + result.usage.completion_tokens,
        "cost_usd": u.cost_usd + cost_usd(result.usage, deps.price_in, deps.price_out),
    })
    await deps.recorder.event(state.run_id, "llm", "reply", {
        "phase": state.phase, "tool_calls": [c.name for c in reply.tool_calls],
        "content": (reply.content or "")[:1000], "finish_reason": result.finish_reason,
        "prompt_tokens": result.usage.prompt_tokens,
        "completion_tokens": result.usage.completion_tokens,
    })
    update: dict = {"messages": [*state.messages, message], "usage": usage}
    if state.phase == "explore":
        update["explore_steps"] = state.explore_steps + 1
    if reply.tool_calls:
        update["route"] = "tools"
    elif state.nudges < MAX_NUDGES:
        update["route"] = "nudge"
    else:
        update |= {"failure_reason": "no_tool_calls", "route": "fail"}
    return update


async def nudge(state: RunState, runtime: Runtime[RunDeps]) -> dict:
    return {"messages": [*state.messages, _user(NUDGE)], "nudges": state.nudges + 1,
            "route": "llm"}


def _calls(message: dict) -> list[ToolCall]:
    return [ToolCall(id=c["id"], name=c["function"]["name"], arguments=c["function"]["arguments"])
            for c in message.get("tool_calls", [])]


async def tools(state: RunState, runtime: Runtime[RunDeps]) -> dict:
    deps = runtime.context
    ctx = ToolContext(sandbox=deps.sandbox, sandbox_id=state.sandbox_id,
                      policy=ToolPolicy(state.protected_paths), test_command=state.test_command)
    messages = list(state.messages)
    usage = state.usage.model_copy()
    phase, plan, summary, finish_called = state.phase, state.plan, state.summary, False
    for call in _calls(state.messages[-1]):
        allowed = allowed_tool_names(state.model_copy(update={"phase": phase}), deps.registry)
        if call.name not in allowed:
            content = (f"ERROR: tool {call.name!r} is not available in the {phase} phase; "
                       f"available: {', '.join(sorted(allowed))}")
        elif call.name in CONTROL_TOOLS:
            try:
                args = CONTROL_TOOLS[call.name].model_validate_json(call.arguments or "{}")
            except ValidationError as exc:
                content = f"ERROR: invalid arguments: {exc.errors(include_url=False)}"
            else:
                if call.name == "submit_plan":
                    plan, phase = Plan(**args.model_dump()), "edit"
                    content = ("Plan recorded. You now have the editing tools: implement the fix, "
                               "run the tests, then call finish.")
                else:
                    summary, finish_called = args.summary, True
                    content = "Submitted for validation."
            await deps.recorder.event(state.run_id, "tools", call.name, {"arguments": call.arguments})
        else:
            usage.tool_calls += 1
            outcome = await deps.registry.dispatch(ctx, call.name, call.arguments)
            usage.tool_failures += not outcome.ok
            usage.unauthorized_attempts += outcome.denied
            await deps.recorder.tool_call(state.run_id, usage.tool_calls, call, outcome)
            content = outcome.for_model()
        messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
    return {"messages": messages, "usage": usage, "phase": phase, "plan": plan,
            "summary": summary, "finish_called": finish_called,
            "route": "validate" if finish_called else "llm"}


def parse_name_status_z(out: str) -> list[str]:
    """Paths from `git diff --name-status -z`; renames and copies contribute both sides."""
    tokens, paths, i = out.split("\x00"), [], 0
    while i < len(tokens):
        status = tokens[i]
        if not status:
            i += 1
            continue
        width = 2 if status[0] in "RC" else 1
        paths += [p for p in tokens[i + 1:i + 1 + width] if p]
        i += 1 + width
    return paths


async def validate(state: RunState, runtime: Runtime[RunDeps]) -> dict:
    """Harness-side check; the model's claim that it is done is never trusted."""
    deps = runtime.context
    sb, sid = deps.sandbox, state.sandbox_id
    await sb.exec(sid, ["git", "add", "--intent-to-add", "--all"])
    diff = (await sb.exec(sid, ["git", "diff"])).stdout
    changed = parse_name_status_z(
        (await sb.exec(sid, ["git", "diff", "--name-status", "-M", "-z"])).stdout)
    policy = ToolPolicy(state.protected_paths)
    violations = [p for p in changed if policy.check_write(p)]
    usage = state.usage
    if violations:
        report = TestReport(passed=False, reason="protected_paths_changed",
                            changed_files=changed, violations=violations)
        usage = usage.model_copy(
            update={"unauthorized_attempts": usage.unauthorized_attempts + len(violations)})
    elif not diff.strip():
        report = TestReport(passed=False, reason="no_changes")
    else:
        res = await sb.exec(sid, state.test_command, timeout_s=TEST_TIMEOUT_S)
        report = TestReport(
            passed=res.exit_code == 0, reason=None if res.exit_code == 0 else "tests_failing",
            exit_code=res.exit_code, output_tail=(res.stdout + res.stderr)[-OUTPUT_TAIL:],
            changed_files=changed,
        )
    await deps.recorder.event(state.run_id, "validate", "report", report.model_dump())
    update = {"diff": diff, "last_tests": report, "usage": usage, "finish_called": False}
    if report.passed:
        return update | {"route": "approval"}
    if report.reason != "protected_paths_changed" and state.retries < state.budget.max_retries:
        return update | {"route": "diagnose"}
    return update | {"failure_reason": report.reason, "route": "fail"}


async def diagnose(state: RunState, runtime: Runtime[RunDeps]) -> dict:
    report = state.last_tests
    if report and report.reason == "no_changes":
        text = ("Validation failed: you have not changed any files. Make the fix, then call "
                "finish.")
    else:
        text = (f"Validation failed: `{' '.join(state.test_command)}` exited with "
                f"{report.exit_code if report else '?'}. Output (tail):\n"
                f"{report.output_tail if report else ''}\n\nFind the cause, fix it, and call "
                "finish again.")
    return {"messages": [*state.messages, _user(text)], "retries": state.retries + 1,
            "route": "llm"}


async def approval(state: RunState, runtime: Runtime[RunDeps]) -> dict:
    deps = runtime.context
    if state.auto_approve:
        decision = Approval(decision="approve", by="auto")
    else:
        await deps.recorder.status(state.run_id, "awaiting_approval")
        tests = state.last_tests
        value = interrupt({
            "run_id": state.run_id, "diff": state.diff, "summary": state.summary,
            "plan": state.plan.model_dump() if state.plan else None,
            "tests": {"exit_code": tests.exit_code if tests else None,
                      "output_tail": tests.output_tail if tests else ""},
        })
        decision = Approval.model_validate(value)
    await deps.recorder.event(state.run_id, "approval", decision.decision, decision.model_dump())
    if decision.decision == "approve":
        return {"approval": decision, "status": "approved", "route": "open_pr"}
    if decision.comment and state.revisions < 1:
        note = _user(f"The reviewer rejected the change: {decision.comment}\nRevise it, run the "
                     "tests, and call finish again.")
        return {"approval": decision, "messages": [*state.messages, note],
                "revisions": state.revisions + 1, "route": "llm"}
    return {"approval": decision, "status": "rejected", "route": "record"}


async def open_pr(state: RunState, runtime: Runtime[RunDeps]) -> dict:
    url = await runtime.context.pr.open(state)
    return {"pr_url": url, "status": "pr_opened" if url else "completed", "route": "record"}


async def fail(state: RunState, runtime: Runtime[RunDeps]) -> dict:
    return {"status": "failed", "route": "record"}


async def record(state: RunState, runtime: Runtime[RunDeps]) -> dict:
    deps = runtime.context
    await deps.recorder.status(
        state.run_id, state.status, failure_reason=state.failure_reason, pr_url=state.pr_url,
        retries=state.retries, usage=state.usage.model_dump(),
    )
    await deps.recorder.event(state.run_id, "record", state.status,
                              {"failure_reason": state.failure_reason})
    return {"route": "end"}
