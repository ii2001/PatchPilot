"""LangGraph construction and deterministic conditional routing."""

from collections.abc import Callable
import time

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

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


def guarded(node: Callable[[RepairState], dict], llm=None) -> Callable[[RepairState], dict]:
    def run(state: RepairState) -> dict:
        trace = [*state.trace, node.__name__]
        started = time.perf_counter()
        previous_calls = len(getattr(llm, "calls", []))
        try:
            update = node(state)
        except Exception as exc:
            # Adapter/policy errors terminate the run instead of retrying an unsafe operation.
            from .docker_runner import SandboxError

            status = "failed"
            if node.__name__ == "create_draft_pr":
                status = "publish_failed"
            elif isinstance(exc, SandboxError):
                status = "manual_action_required"
            update = {"status": status, "error": f"{node.__name__}: {type(exc).__name__}: {exc}"}
        update.update(trace=trace)
        if not state.simulated:
            update.update(
                elapsed_seconds=state.elapsed_seconds + time.perf_counter() - started,
                llm_calls=[*state.llm_calls, *getattr(llm, "calls", [])[previous_calls:]],
            )
        try:
            RepairState.model_validate({**state.model_dump(), **update})
        except Exception:
            return {"status": "failed", "error": f"{node.__name__}: invalid state update", "trace": trace}
        return update

    return run


def build_graph(github: GitHubClient, llm: LLMClient, runner: TestRunner, *, checkpointer=None, publisher=None) -> CompiledStateGraph:
    nodes = RepairNodes(github, llm, runner)
    graph = StateGraph(RepairState)
    sequence = [
        "collect_context", "reproduce_failure", "classify_failure", "diagnose",
        "generate_patch", "apply_patch", "run_tests", "validate_patch", "report_result",
    ]
    if publisher is not None:
        if checkpointer is None:
            raise ValueError("Approval requires a checkpointer")
        sequence[-1:-1] = ["request_approval", "create_draft_pr"]

        def request_approval(state: RepairState) -> dict:
            from .reporting import result_payload

            # Keep interrupt outside guarded's exception handler so LangGraph can checkpoint it.
            answer = interrupt(result_payload(state, awaiting_approval=True))
            approved = answer is True
            return {"approved": approved, "status": "approved" if approved else "rejected",
                    "trace": [*state.trace, "request_approval"]}

        def create_draft_pr(state: RepairState) -> dict:
            return {**publisher.create_draft_pr(state), "status": "ci_pending"}

        graph.add_node("request_approval", request_approval)
        graph.add_node("create_draft_pr", guarded(create_draft_pr, llm))
    for name in sequence:
        if hasattr(nodes, name):
            graph.add_node(name, guarded(getattr(nodes, name), llm))
    graph.add_edge(START, "collect_context")
    routers = {
        "reproduce_failure": (after_reproduction, ["classify_failure", "report_result"]),
        "classify_failure": (after_classification, ["diagnose", "report_result"]),
        "run_tests": (after_tests, ["validate_patch", "diagnose", "report_result"]),
    }
    if publisher is not None:
        routers["request_approval"] = (
            lambda state: "create_draft_pr" if state.approved else "report_result",
            ["create_draft_pr", "report_result"],
        )
    for name, next_name in zip(sequence, sequence[1:]):
        if name in routers:
            router, destinations = routers[name]
            graph.add_conditional_edges(name, router, destinations)
        else:
            def route(state: RepairState, destination: str = next_name) -> str:
                return "report_result" if state.status in {"failed", "manual_action_required", "not_reproduced"} else destination

            graph.add_conditional_edges(name, route, list(dict.fromkeys([next_name, "report_result"])))
    graph.add_edge("report_result", END)
    return graph.compile(checkpointer=checkpointer)
