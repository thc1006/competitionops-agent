"""LangGraph state schema for the CompetitionOps workflow.

A single ``TypedDict`` with ``total=False`` so each node only writes the
keys it owns. Nested Pydantic objects are stored as ``dict``s
(``model_dump(mode="json")``) so the graph can be checkpointed to any
backend that supports JSON serialisation, not just MemorySaver.
"""

from __future__ import annotations

from typing import Any, TypedDict


class CompetitionOpsState(TypedDict, total=False):
    # ---- caller-supplied inputs ----
    raw_brief_text: str
    """Raw competition brief text fed to ``BriefExtractor``."""

    source_uri: str | None
    """Provenance label for the brief (e.g., ``test://runspace``)."""

    team_capacity: list[dict[str, Any]]
    """List of ``TeamMember.model_dump()`` dicts."""

    actor: str
    """``approved_by`` / ``executed_by`` recorded in audit log."""

    # ---- written by ``extract`` node ----
    brief: dict[str, Any] | None

    # ---- written by ``plan`` node ----
    plan: dict[str, Any] | None

    # ---- written by caller via ``graph.update_state`` after interrupt ----
    approved_action_ids: list[str]

    # ---- written by ``approve`` node ----
    rejected_action_ids: list[str]

    # ---- written by ``execute`` node ----
    executed: list[dict[str, Any]]
    skipped: list[dict[str, Any]]
    failed: list[dict[str, Any]]
    blocked: list[dict[str, Any]]

    # ---- written by ``audit`` node ----
    audit_records: list[dict[str, Any]]
