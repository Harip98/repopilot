"""The agent state machine.

    intake -> llm <-> tools          explore: read tools + submit_plan; edit: all + finish
              |  \\-> nudge -> llm    (text-only replies, at most three in a row)
              |       tools --finish--> validate --pass--> request_review -> approval
              |                            |  \\--fail--> diagnose -> llm (bounded retries)
              \\--budget--> fail <---------+     approval --approve--> open_pr -> record
                              \\-> record -> END   approval --reject+comment--> llm (once)
"""

from langgraph.graph import END, START, StateGraph

from repopilot.agent import nodes
from repopilot.agent.deps import RunDeps
from repopilot.agent.state import RunState
from repopilot.telemetry import traced_node

EDGES: dict[str, list[str]] = {
    "intake": ["llm"],
    "llm": ["tools", "nudge", "fail"],
    "nudge": ["llm"],
    "tools": ["llm", "validate"],
    "validate": ["request_review", "diagnose", "fail"],
    "request_review": ["approval"],
    "diagnose": ["llm"],
    "approval": ["open_pr", "llm", "record"],
    "open_pr": ["record"],
    "fail": ["record"],
    "record": [END],
}


def _follow_route(state: RunState) -> str:
    return END if state.route == "end" else state.route


def build_graph() -> StateGraph:
    graph = StateGraph(RunState, context_schema=RunDeps)
    for name in EDGES:
        graph.add_node(name, traced_node(name, getattr(nodes, name)))
    graph.add_edge(START, "intake")
    for name, targets in EDGES.items():
        graph.add_conditional_edges(name, _follow_route, targets)
    return graph
