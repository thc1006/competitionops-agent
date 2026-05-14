"""Locks CLAUDE rule 8: Pydantic model is the single source of truth for API schema.

If anyone reverts an endpoint to accept a raw ``dict[str, str]`` (which
produces an inline ``additionalProperties`` schema), this test fails.
"""

from typing import Any

from fastapi.testclient import TestClient

from competitionops.main import app

_EXPECTED = {
    "/briefs/extract": "BriefExtractRequest",
    "/plans/generate": "PlanGenerateRequest",
    "/plans/{plan_id}/approve": "ApprovalRequest",
}

_REQUIRED_COMPONENTS = (
    "BriefExtractRequest",
    "PlanGenerateRequest",
    "ApprovalRequest",
    "CompetitionBrief",
    "ActionPlan",
    "ApprovalResponse",
)


def _spec() -> dict[str, Any]:
    response = TestClient(app).get("/openapi.json")
    assert response.status_code == 200
    return response.json()


def test_openapi_request_bodies_reference_pydantic_models() -> None:
    spec = _spec()
    paths = spec["paths"]
    for path, expected_schema in _EXPECTED.items():
        assert path in paths, f"missing path {path}"
        post_op = paths[path]["post"]
        schema = post_op["requestBody"]["content"]["application/json"]["schema"]
        assert "$ref" in schema, (
            f"{path} request body lacks $ref — likely a raw dict body, violates "
            "CLAUDE rule 8"
        )
        assert schema["$ref"].endswith(f"/{expected_schema}"), (
            f"{path} should $ref {expected_schema}, got {schema['$ref']}"
        )


def test_openapi_has_required_component_schemas() -> None:
    spec = _spec()
    components = spec.get("components", {}).get("schemas", {})
    for name in _REQUIRED_COMPONENTS:
        assert name in components, f"missing component schema: {name}"


def test_openapi_no_request_body_uses_additional_properties_only() -> None:
    """Defensive: no POST endpoint with a JSON body should fall back to a
    bare ``additionalProperties`` request body. Such a body means an
    untyped dict slipped through.

    ``multipart/form-data`` request bodies (P2-005 PDF upload) are
    exempt — they use FastAPI's ``File()`` / ``UploadFile`` mechanism,
    not a Pydantic body model. The CLAUDE rule 8 source-of-truth check
    only applies to JSON payloads.
    """
    spec = _spec()
    for path, path_item in spec["paths"].items():
        for method, op in path_item.items():
            if not isinstance(op, dict):
                continue
            request_body = op.get("requestBody")
            if not request_body:
                continue
            content = request_body.get("content", {})
            json_schema = content.get("application/json", {}).get("schema")
            if json_schema is None:
                # Multipart-only endpoint (e.g., file upload) — no JSON
                # body to validate. The endpoint's typed parameters
                # (UploadFile / File) keep their own contract via
                # FastAPI dependencies.
                continue
            schema = json_schema
            # Acceptable: $ref to a component
            if "$ref" in schema:
                continue
            # Inline schemas must at least name "properties" — additionalProperties-only
            # bodies are how raw `dict[str, X]` parameters render and that's what we ban.
            assert "properties" in schema or "allOf" in schema or "oneOf" in schema, (
                f"{method.upper()} {path} uses an untyped JSON request body schema: {schema}"
            )
