# Windmill integration

Scripts and flow definitions for running the CompetitionOps Agent via
[Windmill](https://www.windmill.dev/), so PMs can drive the
brief → plan → approve → execute pipeline from Windmill's UI instead of
calling the HTTP API by hand.

## Layout

```
infra/windmill/
├── scripts/
│   ├── extract_brief.py          POST /briefs/extract
│   ├── generate_plan.py          POST /plans/generate
│   └── approve_and_execute.py    POST /plans/{plan_id}/approve
└── flows/
    └── competition_pipeline.yaml end-to-end flow with PM approval gate
```

## How it talks to CompetitionOps

Each script reads `WINDMILL_API_BASE` from env (defaults to
`http://localhost:8000`) and makes plain `httpx` requests against the
FastAPI surface. No Python dep beyond stdlib + httpx — Windmill installs
httpx automatically per Windmill convention.

Dev (Windmill ↔ local FastAPI):

```bash
# Terminal 1
uv run uvicorn competitionops.main:app --reload

# Terminal 2 — start Windmill (any local install works)
docker run -d --name windmill ghcr.io/windmill-labs/windmill:main
# Add env: WINDMILL_API_BASE=http://host.docker.internal:8000
```

Prod / staging: set `WINDMILL_API_BASE` to the CompetitionOps service
URL in Windmill's "Variables" or "Resources" panel.

## Importing the flow

In Windmill UI → **Flows** → **New flow** → **Raw editor** → paste the
contents of `flows/competition_pipeline.yaml`. Save. Hit **Run**, fill
in `content` (the brief text), wait for the suspend prompt, pick
`approved_action_ids` from the rendered plan, fill in `approved_by`,
resume.

The suspend step uses Windmill's `required_events: 1` so the flow can
sleep up to 7 days waiting for the PM — which matches the typical
async work cadence of a competition team.

## Tests

`tests/test_windmill_scripts.py` loads each script via `importlib`
(mirrors how Windmill itself loads rawscript modules — by file path,
not Python package) and runs them against an in-process FastAPI app
through `httpx.MockTransport` + `TestClient`. No real socket opens
during pytest.

Eight tests cover:
- input validation per script
- single-script happy paths against `TestClient`
- end-to-end composition (`extract` → `generate` → `approve_and_execute`)
- env-driven base URL switching

## Out of scope

- Windmill integration smoke test: not in the pytest suite. To exercise
  the YAML against a real Windmill instance, follow the dev steps
  above and run a flow manually.
- Windmill secret / variable provisioning: configure
  `WINDMILL_API_BASE` (and any `AUDIT_LOG_DIR` for prod) in Windmill's
  Variables panel, not in this YAML.
