"""StateGraph builder for the CompetitionOps human-in-the-loop workflow.

Topology::

    START → extract → plan → (interrupt here) → approve → execute → audit → END

The graph compiles with ``interrupt_before=["approve"]`` so that
``graph.invoke(initial_state, config)`` runs ``extract`` + ``plan``
and pauses without invoking any adapter. The caller inspects the
proposed plan via ``graph.get_state(config).values["plan"]``, decides
``approved_action_ids``, calls ``graph.update_state(config,
{"approved_action_ids": [...]})`` to merge the decision, and finally
``graph.invoke(None, config)`` to resume — at which point ``approve``,
``execute``, and ``audit`` run in sequence.

A ``MemorySaver`` checkpointer is wired by default so multiple
``invoke`` calls on the same ``thread_id`` share state across the
interrupt. Production deployments can swap in
``SqliteSaver`` / ``PostgresSaver`` without changing the graph shape.
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from competitionops.workflows.nodes import (
    approve_node,
    audit_node,
    execute_node,
    extract_node,
    plan_node,
)
from competitionops.workflows.state import CompetitionOpsState


def build_graph(
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> Any:
    """Compile and return the human-in-the-loop workflow graph.

    ``checkpointer`` defaults to a fresh ``MemorySaver``; tests that
    need cross-instance persistence can pass a single saver shared
    across multiple ``build_graph`` calls.
    """
    builder: StateGraph[CompetitionOpsState, Any, Any, Any] = StateGraph(
        CompetitionOpsState
    )

    builder.add_node("extract", extract_node)
    builder.add_node("plan", plan_node)
    builder.add_node("approve", approve_node)
    builder.add_node("execute", execute_node)
    builder.add_node("audit", audit_node)

    builder.add_edge(START, "extract")
    builder.add_edge("extract", "plan")
    builder.add_edge("plan", "approve")
    builder.add_edge("approve", "execute")
    builder.add_edge("execute", "audit")
    builder.add_edge("audit", END)

    return builder.compile(
        checkpointer=checkpointer or MemorySaver(),
        interrupt_before=["approve"],
    )
