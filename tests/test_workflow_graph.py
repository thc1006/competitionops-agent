"""P2-001 — LangGraph human-in-the-loop workflow contract.

Covers the six-sprint TDD plan from ``docs/10_p2_roadmap.md`` in a
single integration suite:

- state schema round-trips (Sprint 0)
- ``extract`` and ``plan`` nodes populate the right keys (Sprints 1, 2)
- ``interrupt_before=["approve"]`` pauses without touching the adapter
  layer (Sprint 3)
- ``execute`` + ``audit`` nodes run after the caller supplies
  ``approved_action_ids`` (Sprint 4)
- ``MemorySaver`` checkpointer carries state across instance
  reconstruction within the same ``thread_id`` (Sprint 5)
"""

from __future__ import annotations

import json

import pytest
from langgraph.checkpoint.memory import MemorySaver

from competitionops import main as main_module
from competitionops.workflows import CompetitionOpsState, build_graph
from competitionops.workflows.nodes import (
    approve_node,
    audit_node,
    extract_node,
    plan_node,
)

_RUNSPACE_BRIEF = (
    "RunSpace Innovation Challenge 2026\n"
    "Submission deadline: 2026-09-30\n"
    "Final event: 2026-10-15\n"
    "Required deliverables: pitch deck, demo video, prototype.\n"
)


@pytest.fixture(autouse=True)
def _reset_main_singletons() -> None:
    """Each workflow test gets a fresh in-memory plan_repo/audit/registry."""
    main_module._plan_repo.cache_clear()
    main_module._audit_log.cache_clear()
    main_module._registry.cache_clear()


def _initial_state() -> dict[str, object]:
    return {
        "raw_brief_text": _RUNSPACE_BRIEF,
        "source_uri": "test://runspace-workflow",
        "team_capacity": [
            {
                "member_id": "alice",
                "name": "Alice",
                "role": "business",
                "weekly_capacity_hours": 20,
            }
        ],
        "actor": "pm@example.com",
    }


# ---------------------------------------------------------------------------
# Sprint 0 — state schema round-trip
# ---------------------------------------------------------------------------


def test_state_schema_round_trips_through_json() -> None:
    state: CompetitionOpsState = {
        "raw_brief_text": "Hello world",
        "source_uri": "test://x",
        "team_capacity": [{"member_id": "m1", "name": "A", "role": "b"}],
        "actor": "pm@example.com",
        "approved_action_ids": ["act_a", "act_b"],
        "executed": [],
        "skipped": [],
        "failed": [],
        "blocked": [],
    }
    serialised = json.dumps(state)
    restored = json.loads(serialised)
    assert restored == state


# ---------------------------------------------------------------------------
# Sprint 1 — extract_node populates state.brief
# ---------------------------------------------------------------------------


def test_extract_node_populates_brief_from_raw_text() -> None:
    update = extract_node({"raw_brief_text": _RUNSPACE_BRIEF})
    assert "brief" in update
    brief = update["brief"]
    assert brief is not None
    assert brief["name"].startswith("RunSpace")
    assert brief["submission_deadline"].startswith("2026-09-30")


def test_extract_node_passes_source_uri_through() -> None:
    update = extract_node(
        {"raw_brief_text": _RUNSPACE_BRIEF, "source_uri": "test://abc"}
    )
    assert update["brief"]["source_uri"] == "test://abc"


# ---------------------------------------------------------------------------
# Sprint 2 — plan_node populates state.plan with ≥3 actions
# ---------------------------------------------------------------------------


def test_plan_node_calls_planner_and_returns_action_plan() -> None:
    brief_update = extract_node({"raw_brief_text": _RUNSPACE_BRIEF})
    update = plan_node(
        {
            **brief_update,
            "team_capacity": [
                {
                    "member_id": "m1",
                    "name": "Alice",
                    "role": "business",
                    "weekly_capacity_hours": 20,
                }
            ],
        }
    )
    plan = update["plan"]
    assert plan is not None
    assert plan["dry_run"] is True
    assert plan["requires_approval"] is True
    assert len(plan["actions"]) >= 3


def test_plan_node_raises_when_brief_missing() -> None:
    with pytest.raises(ValueError, match="state.brief"):
        plan_node({"team_capacity": []})


# ---------------------------------------------------------------------------
# Sprint 3 — interrupt_before=["approve"] pauses without adapter calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_interrupts_before_approval_node() -> None:
    graph = build_graph()
    config = {"configurable": {"thread_id": "thread-interrupt"}}

    await graph.ainvoke(_initial_state(), config=config)

    snapshot = graph.get_state(config)
    # Graph paused; the next node is ``approve``.
    assert "approve" in snapshot.next
    # extract + plan ran but execute did not — adapters untouched.
    assert snapshot.values["brief"] is not None
    assert snapshot.values["plan"] is not None
    assert snapshot.values.get("executed") in (None, [])
    # No audit records — execute_node never ran.
    plan_id = snapshot.values["plan"]["plan_id"]
    assert main_module._audit_log().list_for_plan(plan_id) == []


# ---------------------------------------------------------------------------
# Sprint 4 — resume after approval drives execute + audit nodes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_resume_after_approval_executes_and_audits() -> None:
    graph = build_graph()
    config = {"configurable": {"thread_id": "thread-resume"}}

    await graph.ainvoke(_initial_state(), config=config)
    paused = graph.get_state(config)
    target_action_id = paused.values["plan"]["actions"][0]["action_id"]

    graph.update_state(config, {"approved_action_ids": [target_action_id]})
    await graph.ainvoke(None, config=config)

    final = graph.get_state(config)
    # Graph reached END.
    assert final.next == ()
    executed = final.values.get("executed") or []
    assert any(item["action_id"] == target_action_id for item in executed)
    # Audit node populated audit_records.
    audit_records = final.values.get("audit_records") or []
    assert audit_records
    assert any(
        rec["action_id"] == target_action_id and rec["status"] == "executed"
        for rec in audit_records
    )


@pytest.mark.asyncio
async def test_graph_resume_records_rejected_action_ids() -> None:
    graph = build_graph()
    config = {"configurable": {"thread_id": "thread-rejected"}}

    await graph.ainvoke(_initial_state(), config=config)
    paused = graph.get_state(config)
    plan = paused.values["plan"]
    target = plan["actions"][0]["action_id"]
    others = [a["action_id"] for a in plan["actions"][1:]]

    graph.update_state(config, {"approved_action_ids": [target]})
    await graph.ainvoke(None, config=config)

    final = graph.get_state(config)
    rejected = set(final.values.get("rejected_action_ids") or [])
    assert rejected == set(others)


# ---------------------------------------------------------------------------
# Sprint 5 — MemorySaver carries state across graph reconstruction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_checkpoint_persists_across_graph_reconstruction() -> None:
    """A shared MemorySaver lets a second ``build_graph`` see the
    interrupted state from the first ``build_graph`` invocation — same
    contract a production restart with a SQLite/Postgres checkpointer
    would offer (no plan_repo state survives, but the workflow state does).
    """
    saver = MemorySaver()
    config = {"configurable": {"thread_id": "thread-persist"}}

    graph_a = build_graph(checkpointer=saver)
    await graph_a.ainvoke(_initial_state(), config=config)
    snap_a = graph_a.get_state(config)
    assert "approve" in snap_a.next
    plan_id = snap_a.values["plan"]["plan_id"]

    # Drop graph_a entirely. A "new process" rebuilds the graph against
    # the same saver and reads the same paused state for the same
    # thread_id.
    del graph_a
    graph_b = build_graph(checkpointer=saver)
    snap_b = graph_b.get_state(config)
    assert "approve" in snap_b.next
    assert snap_b.values["plan"]["plan_id"] == plan_id


# ---------------------------------------------------------------------------
# Sprint 4 — defense in depth: dangerous action injection still blocked
# ---------------------------------------------------------------------------


def test_approve_node_derives_rejected_set_from_plan() -> None:
    plan_dict = {
        "plan_id": "plan_dummy",
        "competition_id": "c1",
        "actions": [
            {
                "action_id": "act_a",
                "type": "google.drive.create_competition_folder",
                "target_system": "google_drive",
                "payload": {},
                "requires_approval": True,
                "risk_level": "medium",
                "approved": False,
                "status": "pending",
            },
            {
                "action_id": "act_b",
                "type": "google.docs.create_proposal_outline",
                "target_system": "google_docs",
                "payload": {},
                "requires_approval": True,
                "risk_level": "medium",
                "approved": False,
                "status": "pending",
            },
        ],
        "task_drafts": [],
        "dry_run": True,
        "requires_approval": True,
        "risk_level": "medium",
        "risk_flags": [],
    }
    update = approve_node(
        {"plan": plan_dict, "approved_action_ids": ["act_a"]}
    )
    assert update["rejected_action_ids"] == ["act_b"]


def test_audit_node_returns_empty_when_no_plan() -> None:
    """Defensive: if the workflow is invoked with no plan (impossible in
    the happy path but possible in a partial replay), audit_node returns
    an empty list rather than crashing."""
    update = audit_node({})
    assert update == {"audit_records": []}


# ---------------------------------------------------------------------------
# M3 — Reducer annotations on accumulative state fields.
#
# Today the workflow is a single linear chain (extract → plan → approve
# → execute → audit), so the only writer of ``executed`` /
# ``skipped`` / ``failed`` / ``blocked`` / ``audit_records`` is one
# node. Without LangGraph reducer annotations, a future fan-out
# (e.g. ``Send`` API dispatching one execute task per action, or a
# per-record audit pipeline) would silently last-write-wins on these
# lists. Locking the reducer contract NOW keeps that future refactor
# safe.
# ---------------------------------------------------------------------------


def test_accumulative_state_fields_declare_operator_add_reducer() -> None:
    """Structural guard: each accumulative field must be
    ``Annotated[list[dict[str, Any]], operator.add]`` so LangGraph
    merges parallel writes by appending rather than replacing."""
    import operator
    from typing import get_args, get_origin, get_type_hints

    from competitionops.workflows.state import CompetitionOpsState

    hints = get_type_hints(CompetitionOpsState, include_extras=True)
    accumulative = (
        "executed",
        "skipped",
        "failed",
        "blocked",
        "audit_records",
    )
    for field in accumulative:
        annotation = hints[field]
        metadata = getattr(annotation, "__metadata__", ())
        assert metadata, (
            f"{field!r} must be ``Annotated[...]`` to carry a reducer"
        )
        assert operator.add in metadata, (
            f"M3: {field!r} must declare ``operator.add`` as its "
            f"LangGraph reducer. Got metadata={metadata!r}. Without "
            "this, a future fan-out (Send / parallel sub-graphs) "
            "would silently last-write-wins on this list."
        )
        # The underlying type must still be ``list[...]`` so downstream
        # code (model_dump / JSON checkpointing) works.
        underlying = get_args(annotation)[0]
        assert get_origin(underlying) is list, (
            f"{field!r} underlying type must be list, got {underlying!r}"
        )


def test_non_accumulative_state_fields_remain_unannotated() -> None:
    """Defence against over-application: fields that are single-writer
    (caller inputs, extract / plan / approve outputs) must NOT carry an
    additive reducer. Otherwise re-invoking the graph with the same
    thread_id would accumulate duplicates (e.g. ``brief`` becoming
    ``[brief1, brief2]``) which is nonsense for those fields."""
    import operator
    from typing import get_type_hints

    from competitionops.workflows.state import CompetitionOpsState

    hints = get_type_hints(CompetitionOpsState, include_extras=True)
    single_writer = (
        "raw_brief_text",
        "source_uri",
        "team_capacity",
        "actor",
        "brief",
        "plan",
        "approved_action_ids",
        "rejected_action_ids",
    )
    for field in single_writer:
        annotation = hints[field]
        metadata = getattr(annotation, "__metadata__", ())
        assert operator.add not in metadata, (
            f"M3: {field!r} should NOT have operator.add — it's a "
            "single-writer field. Adding a reducer would cause "
            "duplicates on graph replay."
        )


def test_executed_field_accumulates_across_parallel_writers() -> None:
    """Behavioural proof of the M3 contract: two parallel writers
    dispatched via LangGraph's ``Send`` API BOTH contribute their
    record to the final ``executed`` list. Without the reducer this
    would either raise ``InvalidUpdateError`` or silently keep only
    one writer's value.
    """
    # Local imports — Send must not be a function-scoped name when
    # LangGraph reflects on ``dispatcher``'s return annotation, so
    # ``dispatcher`` deliberately has no return annotation.
    from langgraph.graph import END, StateGraph
    from langgraph.types import Send

    from competitionops.workflows.state import CompetitionOpsState

    def dispatcher(state):  # noqa: ANN001, ANN202 — see note above
        # Fan-out: each "action" goes to its own writer via Send.
        return [
            Send("writer", {"action_id": "act_a"}),
            Send("writer", {"action_id": "act_b"}),
        ]

    def writer(payload: dict[str, str]) -> dict[str, list[dict[str, str]]]:
        return {"executed": [{"action_id": payload["action_id"]}]}

    builder: StateGraph = StateGraph(CompetitionOpsState)
    builder.add_node("entry", lambda s: {})  # no-op pass-through
    builder.add_node("writer", writer)
    builder.add_conditional_edges("entry", dispatcher, ["writer"])
    builder.add_edge("writer", END)
    builder.set_entry_point("entry")
    graph = builder.compile()

    result = graph.invoke({})
    executed_ids = {item["action_id"] for item in result.get("executed", [])}
    assert executed_ids == {"act_a", "act_b"}, (
        f"M3: parallel writers via Send must accumulate via the "
        f"operator.add reducer. Got executed={result.get('executed')!r}"
    )
