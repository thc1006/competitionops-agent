# 08 — API Contracts

## GET /health

Liveness probe. Also exposed as `GET /healthz` for k8s compatibility.

Response (`200 OK`):

```json
{ "status": "ok" }
```

## POST /briefs/extract

Read-only: extracts a structured competition brief from untrusted text. No
external write occurs.

Request:

```json
{
  "source_type": "text",
  "source_uri": "drive://file-id or https://example.com/brief.pdf",
  "content": "competition brief text"
}
```

- `source_type`: only `"text"` is supported in MVP. Drive/URL ingestion lands
  in P1-006.
- `source_uri`: optional provenance pointer.
- `content`: required, must be non-empty (`min_length=1`).

Response (`200 OK`):

```json
{
  "competition_id": "runspace-2026",
  "name": "RunSpace Innovation Challenge",
  "organizer": "TBD",
  "source_uri": "drive://file-id",
  "submission_deadline": "2026-06-15T23:59:00+08:00",
  "deliverables": [],
  "scoring_rubric": [],
  "risk_flags": []
}
```

Errors:
- `422 Unprocessable Entity` — missing `content`, empty `content`, or
  unsupported `source_type`.

## POST /plans/generate

Produces an `ActionPlan` from a `CompetitionBrief` plus optional team
capacity. The plan is **persisted in the local in-memory store** so that a
follow-up approval / execution call can reference it by `plan_id`. No
external write occurs at this stage.

Request:

```json
{
  "competition": { "...CompetitionBrief..." },
  "team_capacity": [
    {
      "member_id": "m1",
      "name": "Alice",
      "role": "business",
      "weekly_capacity_hours": 20
    }
  ],
  "preferences": {
    "calendar_name": "Startup Competition Deadlines",
    "pm_approval_required": true
  }
}
```

Response (`200 OK`):

```json
{
  "plan_id": "plan_01H...",
  "competition_id": "runspace-2026",
  "dry_run": true,
  "requires_approval": true,
  "actions": [],
  "task_drafts": [],
  "risk_flags": [],
  "risk_level": "medium"
}
```

Every action in `actions` starts in `status: "pending"`.

## POST /plans/{plan_id}/approve — legacy combined endpoint

One-shot dry-run approval + execution. Use this when you want the
"approve & run" semantics in a single HTTP call. The two-phase API
(`/approvals/...` + `/executions/...`) is preferred for production flows
because it gives PM a discrete preview between approval and execution.

Request:

```json
{
  "approved_action_ids": ["act_001", "act_002"],
  "approved_by": "pm@example.com",
  "allow_reexecute": false
}
```

Response (`200 OK`):

```json
{
  "plan_id": "plan_01H...",
  "executed": [],
  "skipped": [],
  "failed": [],
  "blocked": []
}
```

Errors:
- `404 Not Found` — unknown `plan_id`.

## POST /approvals/{plan_id}/approve — approve-only

Pure approval: transitions `action.status` from `pending` to `approved`
(or `rejected` for actions not in `approved_action_ids`; or `blocked`
for forbidden action types) **without** calling any adapter. Audit log
records the approval/rejection decision per action.

Request: same shape as legacy combined endpoint (`ApprovalRequest`), but
`allow_reexecute` is ignored at this phase.

Response (`200 OK`, `ApprovalDecision`):

```json
{
  "plan_id": "plan_01H...",
  "approved": ["act_001"],
  "rejected": ["act_002", "act_003"],
  "blocked": [],
  "skipped": []
}
```

`skipped` here refers to actions whose `status` is already `executed` —
no state change.

Errors:
- `404 Not Found` — unknown `plan_id`.
- `422 Unprocessable Entity` — malformed body.

## POST /executions/{plan_id}/run — run-only

Executes actions that are already in `approved` status. Refuses to execute
anything in any other status; refused actions surface in `skipped` with a
message pointing the caller back to `/approvals/{plan_id}/approve`.

Request (`ExecutionRequest`):

```json
{
  "executed_by": "pm@example.com",
  "action_ids": ["act_001"],
  "allow_reexecute": false
}
```

- `action_ids`: optional. If omitted, every action with `status==approved`
  is run.
- `allow_reexecute`: when `true`, actions in `status==executed` are re-run.

Response (`200 OK`, `ApprovalResponse`):

```json
{
  "plan_id": "plan_01H...",
  "executed": [],
  "skipped": [],
  "failed": [],
  "blocked": []
}
```

Errors:
- `404 Not Found` — unknown `plan_id`.
- `422 Unprocessable Entity` — malformed body.

## ExternalAction

```json
{
  "action_id": "act_001",
  "type": "google.calendar.create_event",
  "target_system": "google_calendar",
  "payload": {},
  "requires_approval": true,
  "risk_level": "medium",
  "status": "pending",
  "approved": false
}
```

`status` values: `pending`, `approved`, `rejected`, `blocked`, `executed`,
`failed`. The transitions are enforced by `ExecutionService`.

## FORBIDDEN_ACTION_TYPES

The following action types are blocked at both `/approvals` and
`/executions` regardless of approval intent, and are never exposed as MCP
tools. They map to the "forbidden in MVP" rows of
`docs/05_security_oauth.md` plus defensive extensions:

- `google.drive.delete_file`
- `google.drive.permissions.set_public`
- `google.drive.permissions.share_external`
- `gmail.send`
- `competition.external_submit`
