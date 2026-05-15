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

- `source_type`: only `"text"` is supported by this endpoint. PDF goes
  through `POST /briefs/extract/pdf` (P2-005). URL goes through
  `POST /briefs/extract/url` (P1-006, see below).
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

## POST /briefs/extract/url

P1-006 — Sprint 0. Fetch a URL via the web ingestion port and return
a structured `CompetitionBrief`. Sprint 0 ships with a mock adapter
only (canned content + deterministic synthetic results); Sprint 2
swaps in a real Crawl4AI / Playwright adapter behind `WEB_ADAPTER=crawl4ai`.

Request:

```json
{
  "url": "https://example.com/competition"
}
```

- `url`: required. Scheme MUST be `http` or `https` (validated at
  Pydantic layer). `file://`, `javascript:`, `data:`, `ftp:` return 422.
- The adapter resolves the URL's canonical (post-redirect) form;
  the response's `source_uri` reflects that canonical URL, which
  may differ from the request `url`.

Response (`200 OK`): same `CompetitionBrief` shape as
`/briefs/extract`.

Errors:
- `422 Unprocessable Entity` — missing/empty `url`, or scheme not
  `http(s)`.
- `500 Internal Server Error` — adapter raises (network failure with
  the real adapter, or `WEB_ADAPTER=crawl4ai` while still in Sprint
  0 / without `--extra web`).

## POST /briefs/extract/drive

P2-005 Sprint 5 — pull a PDF from a Google Drive file id and return a
structured `CompetitionBrief`. Reuses the `/briefs/extract/pdf`
pipeline (same `PDF_ADAPTER` engine, same 10 MiB cap, same `%PDF-`
magic gate) and stamps `source_uri = drive://<file_id>`.

Reading a Drive file is low-risk (CLAUDE.md rule #4's approval gate
covers move / delete / permission changes, not reads) — no dry_run.

Request:

```json
{
  "file_id": "1AbCdEf...drive-file-id"
}
```

- `file_id`: required, non-empty / non-whitespace.
- Real Drive access activates when `Settings.google_oauth_access_token`
  is set; otherwise the mock adapter returns canned bytes.

Response (`200 OK`): same `CompetitionBrief` shape as
`/briefs/extract`, with `source_uri = "drive://<file_id>"`.

Errors:
- `422 Unprocessable Entity` — missing/empty `file_id`, or the
  downloaded file does not start with the `%PDF-` magic bytes.
- `413 Content Too Large` — the Drive file exceeds the 10 MiB cap.
- `404 Not Found` — Drive has no file with that id.
- `502 Bad Gateway` — Drive returned a non-404 error status, or a
  network-class failure during the download (the exception class name
  surfaces; the body is redacted).

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
