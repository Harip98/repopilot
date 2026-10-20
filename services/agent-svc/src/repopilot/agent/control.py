"""Control tools: handled by the graph itself, they move the run between phases."""

from pydantic import BaseModel, Field

from repopilot.agent.state import RunState
from repopilot.llm.types import ToolSpec
from repopilot.tools.registry import ToolRegistry, drop_titles


class SubmitPlanArgs(BaseModel):
    root_cause: str = Field(description="What is wrong and why, in a few sentences.")
    files: list[str] = Field(description="Files you expect to change.")
    acceptance_criteria: list[str] = Field(description="Observable behaviour once fixed.")
    test_targets: list[str] = Field(default_factory=list,
                                    description="Tests you will add or run to prove the fix.")


class FinishArgs(BaseModel):
    summary: str = Field(description="What you changed and why, for the reviewer.")


def _spec(name: str, description: str, model: type[BaseModel]) -> ToolSpec:
    return ToolSpec(name=name, description=description,
                    parameters=drop_titles(model.model_json_schema()))


SUBMIT_PLAN = _spec("submit_plan", "Record your diagnosis and plan; unlocks the editing tools.",
                    SubmitPlanArgs)
FINISH = _spec("finish", "Submit your change for validation by the full test suite.", FinishArgs)
CONTROL_TOOLS = {SUBMIT_PLAN.name: SubmitPlanArgs, FINISH.name: FinishArgs}


def tools_for(state: RunState, registry: ToolRegistry) -> list[ToolSpec]:
    if state.phase == "explore":
        if state.explore_steps >= state.budget.max_explore_steps:
            return [SUBMIT_PLAN]  # exploration budget spent: the plan is due now
        return registry.subset({"read"}).specs() + [SUBMIT_PLAN]
    return registry.specs() + [FINISH]


def allowed_tool_names(state: RunState, registry: ToolRegistry) -> set[str]:
    return {s.name for s in tools_for(state, registry)}
