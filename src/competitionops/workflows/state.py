"""LangGraph state schema for the CompetitionOps workflow.

A single ``TypedDict`` with ``total=False`` so each node only writes the
keys it owns. Nested Pydantic objects are stored as ``dict``s
(``model_dump(mode="json")``) so the graph can be checkpointed to any
backend that supports JSON serialisation, not just MemorySaver.

Reducer policy (deep-review M3):

The default LangGraph channel for a state field is "last value wins" —
two parallel writes to the same key per step raise
``InvalidUpdateError``. That's the right semantics for single-writer
fields like ``brief`` or ``plan``: any duplicate write is a real bug.

For fields that accumulate results across multiple writers — namely
the four execution outcome lists (``executed`` / ``skipped`` /
``failed`` / ``blocked``) and the ``audit_records`` snapshot — we
explicitly annotate them with ``operator.add`` so LangGraph appends
parallel writes instead of failing. The current graph is linear so
only one node writes each list per invocation, but locking the
reducer contract NOW means a future fan-out (e.g. one execute task
per action via the ``Send`` API) will just work without revisiting
this schema.

Single-writer fields stay unannotated on purpose: adding a reducer
to ``brief``/``plan``/etc would cause duplicates to accumulate on
graph replay, which is nonsense for those fields.

Snapshot-vs-delta invariant (round-2 M5):

The ``operator.add`` reducer ONLY behaves correctly when each
writer emits its own DELTA (the new elements produced BY THIS
writer), not a SNAPSHOT (the full current contents of the
upstream store). Today the linear graph runs ``execute_node`` and
``audit_node`` exactly once per invocation, so both are free to
return full snapshots — ``approve_and_execute`` gives the whole
response, ``list_for_plan`` gives every audit record — and the
reducer trivially collapses to "one writer, identity merge".

The hazard is the FIRST time someone adds a ``Send``-based fan-out
(e.g. one parallel sub-task per action). Each sub-task naturally
re-queries the audit log / execution service and gets back the
SAME full snapshot. The reducer then concatenates N copies of the
same data → double / triple / N-tuple counting. The fix is NOT
to weaken the reducer (correct for true deltas), it is to
restructure the producer node so each sub-task returns only its
own delta (e.g. a single-element list with the result of ITS
action, never the global list).

Future fan-out authors: do NOT just wrap the existing node body
in ``Send`` and call it done. The node must be refactored to
return per-task deltas first, then the fan-out works.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class CompetitionOpsState(TypedDict, total=False):
    # ---- caller-supplied inputs (single-writer, default reducer) ----
    raw_brief_text: str
    """Raw competition brief text fed to ``BriefExtractor``."""

    source_uri: str | None
    """Provenance label for the brief (e.g., ``test://runspace``)."""

    team_capacity: list[dict[str, Any]]
    """List of ``TeamMember.model_dump()`` dicts."""

    actor: str
    """``approved_by`` / ``executed_by`` recorded in audit log."""

    # ---- written by ``extract`` node (single-writer) ----
    brief: dict[str, Any] | None

    # ---- written by ``plan`` node (single-writer) ----
    plan: dict[str, Any] | None

    # ---- written by caller via ``graph.update_state`` after interrupt ----
    approved_action_ids: list[str]

    # ---- written by ``approve`` node (single-writer) ----
    rejected_action_ids: list[str]

    # ---- written by ``execute`` node — additive reducer (M3) ----
    # ``operator.add`` makes parallel writes append instead of clash.
    # Required for any future Send-based fan-out where each action
    # dispatches as its own sub-task and produces one element here.
    executed: Annotated[list[dict[str, Any]], operator.add]
    skipped: Annotated[list[dict[str, Any]], operator.add]
    failed: Annotated[list[dict[str, Any]], operator.add]
    blocked: Annotated[list[dict[str, Any]], operator.add]

    # ---- written by ``audit`` node — additive reducer (M3) ----
    audit_records: Annotated[list[dict[str, Any]], operator.add]
