"""LangGraph construction and deterministic conditional routing."""

from collections.abc import Callable

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from .adapters import GitHubClient, LLMClient, TestRunner
from .nodes import RepairNodes
from .state import MAX_ATTEMPTS, RepairState


def after_reproduction(state: RepairState) -> str:
    return "report_result" if state.status != "pending" else "classify_failure"


def after_classification(state: RepairState) -> str:
    return "report_result" if state.status != "pending" else "diagnose"


def after_tests(state: RepairState) -> str:
    if state.status != "pending":
        return "report_result"
    if state.test_results[-1].outcome == "passed":
        return "validate_patch"
    return "diagnose" if state.attempt < MAX_ATTEMPTS else "report_result"


def guarded(node: Callable[[RepairState], dict]) -> Callable[[RepairState], dict]:
    def run(state: RepairState) -> dict:
        trace = [*state.trace, node.__name__]
        try:
            update = node(state)
            RepairState.model_validate({**state.model_dump(), **update, "trace": trace})
            return {**update, "trace": trace}
        except Exception as exc:
            # Adapter/policy errors terminate the run instead of retrying an unsafe operation.
            return {"status": "failed", "error": f"{node.__name__}: {type(exc).__name__}: {exc}", "trace": trace}

    return run


def build_graph(github: GitHubClient, llm: LLMClient, runner: TestRunner) -> CompiledStateGraph:
    nodes = RepairNodes(github, llm, runner)
    graph = StateGraph(RepairState)
    sequence = [
        "collect_context", "reproduce_failure", "classify_failure", "diagnose",
        "generate_patch", "apply_patch", "run_tests", "validate_patch", "report_result",
    ]
    for name in sequence:
        graph.add_node(name, guarded(getattr(nodes, name)))
    graph.add_edge(START, "collect_context")
    routers = {
        "reproduce_failure": (after_reproduction, ["classify_failure", "report_result"]),
        "classify_failure": (after_classification, ["diagnose", "report_result"]),
        "run_tests": (after_tests, ["validate_patch", "diagnose", "report_result"]),
    }
    for name, next_name in zip(sequence, sequence[1:]):
        if name in routers:
            router, destinations = routers[name]
            graph.add_conditional_edges(name, router, destinations)
        else:
            def route(state: RepairState, destination: str = next_name) -> str:
                return "report_result" if state.status == "failed" else destination

            graph.add_conditional_edges(name, route, list(dict.fromkeys([next_name, "report_result"])))
    graph.add_edge("report_result", END)
    return graph.compile()
