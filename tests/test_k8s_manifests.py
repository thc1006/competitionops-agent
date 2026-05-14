"""P2-003 — Kubernetes manifest contract.

Parses the YAML files under ``infra/k8s/`` directly (no kustomize CLI
required for the bulk of the suite) and asserts the production-posture
invariants — distroless image, nonroot pod, dropped capabilities,
PVC-backed audit log mount, secret-template completeness, overlay
specialisation (emptyDir for dev, PVC for staging/prod, ingress TLS).

The single ``kustomize build`` smoke test is auto-skipped when the
``kustomize`` binary is not on PATH, so the suite stays green on
machines without it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_K8S = _REPO_ROOT / "infra" / "k8s"
_BASE = _K8S / "base"
_OVERLAYS = _K8S / "overlays"
_DOCKERFILE = _REPO_ROOT / "infra" / "docker" / "Dockerfile"


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _load_all_yaml(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [doc for doc in yaml.safe_load_all(handle) if doc is not None]


# ---------------------------------------------------------------------------
# Base — kustomization manifest list
# ---------------------------------------------------------------------------


def test_kustomize_base_lists_all_required_resources() -> None:
    kustomization = _load_yaml(_BASE / "kustomization.yaml")
    listed = set(kustomization["resources"])
    expected = {
        "namespace.yaml",
        "deployment.yaml",
        "service.yaml",
        "configmap.yaml",
        "secret.template.yaml",
        "pvc.yaml",
    }
    assert expected.issubset(listed), (
        f"missing from base/kustomization.yaml: {expected - listed}"
    )


def test_kustomize_base_targets_competitionops_namespace() -> None:
    kustomization = _load_yaml(_BASE / "kustomization.yaml")
    assert kustomization["namespace"] == "competitionops"


# ---------------------------------------------------------------------------
# Deployment — distroless + nonroot + read-only root + capability drop
# ---------------------------------------------------------------------------


def _base_deployment() -> dict[str, Any]:
    return _load_yaml(_BASE / "deployment.yaml")


def test_base_deployment_image_references_distroless() -> None:
    container = _base_deployment()["spec"]["template"]["spec"]["containers"][0]
    assert "distroless" in container["image"], (
        f"expected distroless base image, got {container['image']!r}"
    )


def test_base_deployment_pod_runs_as_nonroot() -> None:
    pod = _base_deployment()["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["runAsUser"] != 0


def test_base_deployment_container_drops_all_capabilities() -> None:
    container = _base_deployment()["spec"]["template"]["spec"]["containers"][0]
    drops = container["securityContext"]["capabilities"]["drop"]
    assert "ALL" in drops


def test_base_deployment_container_disallows_privilege_escalation() -> None:
    container = _base_deployment()["spec"]["template"]["spec"]["containers"][0]
    assert container["securityContext"]["allowPrivilegeEscalation"] is False


def test_base_deployment_container_uses_read_only_root_fs() -> None:
    container = _base_deployment()["spec"]["template"]["spec"]["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True


def test_base_deployment_disables_service_account_token_automount() -> None:
    pod = _base_deployment()["spec"]["template"]["spec"]
    # automountServiceAccountToken=false eliminates a CSRF/SSRF
    # escalation vector if the api ever gets compromised.
    assert pod["automountServiceAccountToken"] is False


# ---------------------------------------------------------------------------
# Deployment — probes hit the right endpoints (Sprint 5 P2-004 confirmed
# these paths and Sprint 6 main.py exposes both)
# ---------------------------------------------------------------------------


def test_base_deployment_readiness_probe_hits_health() -> None:
    container = _base_deployment()["spec"]["template"]["spec"]["containers"][0]
    probe = container["readinessProbe"]["httpGet"]
    assert probe["path"] == "/health"


def test_base_deployment_liveness_probe_hits_healthz() -> None:
    container = _base_deployment()["spec"]["template"]["spec"]["containers"][0]
    probe = container["livenessProbe"]["httpGet"]
    assert probe["path"] == "/healthz"


# ---------------------------------------------------------------------------
# Deployment — env + audit log PVC mount (Tier 0 #4)
# ---------------------------------------------------------------------------


def test_base_deployment_loads_env_from_configmap_and_secret() -> None:
    container = _base_deployment()["spec"]["template"]["spec"]["containers"][0]
    envFrom = container.get("envFrom") or []
    names = {ref.get("configMapRef", {}).get("name") for ref in envFrom} | {
        ref.get("secretRef", {}).get("name") for ref in envFrom
    }
    assert "competitionops-config" in names
    assert "competitionops-secrets" in names


def test_base_deployment_mounts_audit_pvc_at_var_lib_competitionops() -> None:
    container = _base_deployment()["spec"]["template"]["spec"]["containers"][0]
    mounts = container["volumeMounts"]
    audit_mounts = [
        m for m in mounts if m["mountPath"] == "/var/lib/competitionops/audit"
    ]
    assert len(audit_mounts) == 1, "audit mount path must be exactly one"
    assert audit_mounts[0]["name"] == "audit-log"

    volumes = _base_deployment()["spec"]["template"]["spec"]["volumes"]
    audit_volume = next(v for v in volumes if v["name"] == "audit-log")
    assert "persistentVolumeClaim" in audit_volume
    assert audit_volume["persistentVolumeClaim"]["claimName"] == "competitionops-audit"


def test_base_pvc_is_rwx_with_audit_size_request() -> None:
    pvc = _load_yaml(_BASE / "pvc.yaml")
    assert pvc["kind"] == "PersistentVolumeClaim"
    assert "ReadWriteMany" in pvc["spec"]["accessModes"]
    assert pvc["spec"]["resources"]["requests"]["storage"]


def test_base_configmap_sets_audit_log_dir_to_match_mount() -> None:
    cm = _load_yaml(_BASE / "configmap.yaml")
    assert cm["data"]["AUDIT_LOG_DIR"] == "/var/lib/competitionops/audit"


def test_base_configmap_sets_production_defaults() -> None:
    cm = _load_yaml(_BASE / "configmap.yaml")
    # Defense in depth: prod-shape pods stay dry-run + approval-required
    # unless the operator explicitly overrides with an overlay.
    assert cm["data"]["APPROVAL_REQUIRED"] == "true"
    assert cm["data"]["DRY_RUN_DEFAULT"] == "true"


# ---------------------------------------------------------------------------
# Secret template
# ---------------------------------------------------------------------------


def test_secret_template_lists_all_required_keys() -> None:
    secret = _load_yaml(_BASE / "secret.template.yaml")
    stringData = secret["stringData"]
    required = {
        "ANTHROPIC_API_KEY",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "PLANE_API_KEY",
        "PLANE_BASE_URL",
        "PLANE_WORKSPACE_SLUG",
        "PLANE_PROJECT_ID",
    }
    assert required.issubset(stringData.keys()), (
        f"missing keys: {required - set(stringData.keys())}"
    )


def test_secret_template_values_are_empty_placeholders() -> None:
    """Real credentials must never live in this YAML — values are
    populated at deploy time via external-secrets / sealed-secrets /
    kubectl create."""
    secret = _load_yaml(_BASE / "secret.template.yaml")
    for key, value in secret["stringData"].items():
        assert value == "", (
            f"secret.template.yaml field {key!r} must be empty, got {value!r}"
        )


# ---------------------------------------------------------------------------
# Overlays
# ---------------------------------------------------------------------------


def test_overlay_dev_replaces_audit_volume_with_emptydir() -> None:
    patch = _load_yaml(_OVERLAYS / "dev" / "deployment-patch.yaml")
    volumes = patch["spec"]["template"]["spec"]["volumes"]
    audit_volume = next(v for v in volumes if v["name"] == "audit-log")
    assert "emptyDir" in audit_volume
    assert "persistentVolumeClaim" not in audit_volume


def test_overlay_staging_uses_base_pvc_unchanged() -> None:
    """Staging mirrors prod's PVC posture — no audit-volume patch."""
    kustomization = _load_yaml(_OVERLAYS / "staging" / "kustomization.yaml")
    # Staging adds ingress but does NOT patch the deployment.
    assert "ingress.yaml" in kustomization["resources"]
    assert "patches" not in kustomization or not kustomization.get("patches")


def test_overlay_prod_uses_three_replicas_with_anti_affinity() -> None:
    patch = _load_yaml(_OVERLAYS / "prod" / "deployment-patch.yaml")
    assert patch["spec"]["replicas"] == 3
    affinity = patch["spec"]["template"]["spec"]["affinity"]
    assert "podAntiAffinity" in affinity


def test_overlay_prod_ingress_has_tls_block() -> None:
    ingress = _load_yaml(_OVERLAYS / "prod" / "ingress.yaml")
    assert ingress["spec"]["tls"], "prod ingress must define TLS"
    tls = ingress["spec"]["tls"][0]
    assert tls["secretName"]
    assert tls["hosts"]


def test_overlay_prod_ingress_uses_letsencrypt_prod_issuer() -> None:
    ingress = _load_yaml(_OVERLAYS / "prod" / "ingress.yaml")
    annotations = ingress["metadata"]["annotations"]
    assert annotations["cert-manager.io/cluster-issuer"] == "letsencrypt-prod"


def test_overlay_prod_ingress_routes_to_service() -> None:
    ingress = _load_yaml(_OVERLAYS / "prod" / "ingress.yaml")
    rule = ingress["spec"]["rules"][0]
    backend = rule["http"]["paths"][0]["backend"]
    assert backend["service"]["name"] == "competitionops-api"


def test_overlay_staging_ingress_uses_letsencrypt_staging_issuer() -> None:
    ingress = _load_yaml(_OVERLAYS / "staging" / "ingress.yaml")
    annotations = ingress["metadata"]["annotations"]
    assert annotations["cert-manager.io/cluster-issuer"] == "letsencrypt-staging"


def test_each_overlay_targets_distinct_namespace() -> None:
    namespaces = {
        env: _load_yaml(_OVERLAYS / env / "kustomization.yaml")["namespace"]
        for env in ("dev", "staging", "prod")
    }
    assert namespaces == {
        "dev": "competitionops-dev",
        "staging": "competitionops-staging",
        "prod": "competitionops-prod",
    }


# ---------------------------------------------------------------------------
# Dockerfile — distroless multi-stage + nonroot
# ---------------------------------------------------------------------------


def test_dockerfile_uses_distroless_runtime_with_nonroot_tag() -> None:
    content = _DOCKERFILE.read_text(encoding="utf-8")
    assert "gcr.io/distroless/python3-debian12:nonroot" in content


def test_dockerfile_is_multi_stage() -> None:
    """Two FROM lines = builder + runtime separation. Keeps uv /
    pip / apt out of the runtime image."""
    content = _DOCKERFILE.read_text(encoding="utf-8")
    from_lines = [
        line for line in content.splitlines() if line.lstrip().startswith("FROM ")
    ]
    assert len(from_lines) >= 2, (
        f"Dockerfile should be multi-stage; saw FROM lines: {from_lines}"
    )


# ---------------------------------------------------------------------------
# Optional smoke — runs only when the kustomize binary is available
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    shutil.which("kustomize") is None,
    reason="kustomize CLI not installed; manifest YAML parsed above is enough",
)
def test_kustomize_build_succeeds_on_every_overlay() -> None:
    for overlay in ("dev", "staging", "prod"):
        result = subprocess.run(
            ["kustomize", "build", str(_OVERLAYS / overlay)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"kustomize build failed for {overlay}: {result.stderr}"
        )
        assert "kind: Deployment" in result.stdout
