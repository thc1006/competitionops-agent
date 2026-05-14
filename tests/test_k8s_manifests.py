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
        # H1 fix — namespace.yaml moved out of base into each overlay.
        # Base no longer declares a Namespace kind because kustomize's
        # ``namespace:`` field cannot rename one.
        "deployment.yaml",
        "service.yaml",
        "configmap.yaml",
        "secret.template.yaml",
        "pvc.yaml",
    }
    assert expected.issubset(listed), (
        f"missing from base/kustomization.yaml: {expected - listed}"
    )


def test_kustomize_base_is_overlay_only_and_does_not_pin_a_namespace() -> None:
    """Base used to set ``namespace: competitionops`` and ship a matching
    Namespace resource. After the H1 fix, base is overlay-only — the
    ``namespace:`` field is dropped so a base-only consumer is forced to
    pick an overlay (or supply ``--namespace`` to kubectl) rather than
    silently landing in a namespace that no manifest creates."""
    kustomization = _load_yaml(_BASE / "kustomization.yaml")
    assert "namespace" not in kustomization, (
        "base must not pin a namespace anymore — overlays own that. "
        f"Got namespace={kustomization.get('namespace')!r}."
    )


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


def test_overlay_prod_pinned_to_one_replica_until_shared_plan_repo_lands() -> None:
    """H2 regression guard.

    Prod overlay is pinned to ``replicas: 1``. As of the shared-plan-repo
    PR, ``FilePlanRepository`` (file-backed, atomic-rename save, opted
    in via ``PLAN_REPO_DIR``) ships in-tree and closes half the H2
    dependency. The OTHER half is H3 — multi-writer safety for the
    ``FileAuditLog`` JSONL appends, which is still dormant while
    replicas=1. The pin therefore stays at 1 until BOTH:

    1. The operator has wired ``PLAN_REPO_DIR`` onto a shared volume.
    2. H3 is closed (``fcntl.flock`` around appends, or per-pod
       filenames).

    The ``podAntiAffinity`` block stays in the patch so the spread
    intent survives this temporary 1-replica window; it's a no-op now
    but lights up immediately when replicas is bumped.
    """
    patch = _load_yaml(_OVERLAYS / "prod" / "deployment-patch.yaml")
    assert patch["spec"]["replicas"] == 1, (
        "Prod must stay at replicas=1 until BOTH PLAN_REPO_DIR is "
        "wired on a shared volume AND H3 (audit-log multi-writer "
        "safety) is closed. See H2 comment in this test for context."
    )
    affinity = patch["spec"]["template"]["spec"]["affinity"]
    assert "podAntiAffinity" in affinity


def test_overlay_prod_patch_documents_the_h2_pin_in_a_comment() -> None:
    """Defensive: the YAML structure alone doesn't explain WHY prod is
    pinned, so future operators reading the patch might assume the 1
    is a typo and bump it. The patch file must carry an H2 reference
    in a comment so the constraint is visible at the manifest layer."""
    content = (_OVERLAYS / "prod" / "deployment-patch.yaml").read_text(
        encoding="utf-8"
    )
    assert "H2" in content, (
        "deployment-patch.yaml must reference H2 in a comment so the "
        "replicas=1 pin doesn't get silently reverted."
    )
    # The comment should also point at the underlying cause so a reader
    # doesn't have to dig through tests / docs to discover why.
    assert "PlanRepository" in content or "plan_repo" in content, (
        "The H2 comment should name the dependency that has to land "
        "before replicas can go above 1."
    )


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
# H1 regression guard — each overlay must actually CREATE the target
# namespace it points at.
#
# Background: kustomize's ``namespace:`` field only rewrites the
# ``metadata.namespace`` of namespaced resources. It does NOT rename a
# ``Namespace`` kind. Before this fix, ``infra/k8s/base/namespace.yaml``
# shipped ``Namespace/competitionops`` and the three overlays set
# ``namespace: competitionops-{env}``. The rendered output therefore
# declared the wrong namespace (``competitionops``) and tried to put
# the Deployment / Service / PVC into ``competitionops-{env}`` which
# was never created. ``kubectl apply -k overlays/dev/`` would fail.
#
# The fix moves the Namespace resource out of base. Each overlay ships
# its own ``namespace.yaml`` declaring the matching ``Namespace`` kind.
# These tests verify that contract WITHOUT requiring the kustomize CLI,
# so they run in CI regardless of binary availability.
# ---------------------------------------------------------------------------


def _collect_namespace_resources_from_kustomization(
    kustomization_dir: Path,
) -> list[dict[str, Any]]:
    """Walk a kustomization's ``resources:`` list (recursively into
    referenced directories) and return every Namespace resource it
    eventually contributes to the rendered output."""
    kustomization = _load_yaml(kustomization_dir / "kustomization.yaml")
    found: list[dict[str, Any]] = []
    for entry in kustomization.get("resources", []) or []:
        path = (kustomization_dir / entry).resolve()
        if path.is_dir():
            found.extend(_collect_namespace_resources_from_kustomization(path))
            continue
        if not path.is_file():
            continue
        for doc in _load_all_yaml(path):
            if doc.get("kind") == "Namespace":
                found.append(doc)
    return found


def test_each_overlay_declares_a_namespace_resource_matching_its_target() -> None:
    """For every overlay, the rendered manifest set must include a
    ``Namespace`` resource whose ``metadata.name`` equals the overlay's
    ``namespace:`` field. Otherwise ``kubectl apply -k`` will try to
    create namespaced resources before the namespace exists."""
    for env in ("dev", "staging", "prod"):
        overlay_dir = _OVERLAYS / env
        target_ns = _load_yaml(overlay_dir / "kustomization.yaml")["namespace"]

        namespaces = _collect_namespace_resources_from_kustomization(overlay_dir)
        names = [ns["metadata"]["name"] for ns in namespaces]

        assert target_ns in names, (
            f"overlay {env!r} targets namespace {target_ns!r} but the "
            f"rendered resources only declare {names!r}. Kustomize's "
            "`namespace:` field does NOT rename Namespace kinds — each "
            "overlay must ship its own namespace.yaml so the namespace "
            "is actually created."
        )


def test_overlay_namespace_resources_carry_managed_by_kustomize_label() -> None:
    """Sanity: the per-overlay namespace.yaml files should label the
    Namespace consistently with what base used to ship, so observability
    tooling (ArgoCD, Lens, etc.) keeps recognising it."""
    for env in ("dev", "staging", "prod"):
        local_ns_path = _OVERLAYS / env / "namespace.yaml"
        # Existence is asserted by the previous test indirectly (no
        # base namespace.yaml exists after the fix), but make it
        # explicit here for a clearer failure message.
        assert local_ns_path.exists(), (
            f"overlays/{env}/namespace.yaml must exist — base no longer "
            "ships a Namespace resource."
        )
        doc = _load_yaml(local_ns_path)
        assert doc["kind"] == "Namespace"
        assert doc["metadata"]["name"] == f"competitionops-{env}"
        labels = doc["metadata"].get("labels", {})
        assert labels.get("app.kubernetes.io/name") == "competitionops"


def test_base_no_longer_ships_a_namespace_resource() -> None:
    """Base must not declare a Namespace anymore — that responsibility
    moved into the overlays so each env owns its target namespace."""
    base_ns = _BASE / "namespace.yaml"
    assert not base_ns.exists(), (
        f"{base_ns} should have been removed in the H1 fix. Keeping "
        "it here creates an extra `Namespace/competitionops` resource "
        "in every overlay's rendered output that the env never uses."
    )
    base_kust = _load_yaml(_BASE / "kustomization.yaml")
    assert "namespace.yaml" not in (base_kust.get("resources") or []), (
        "base/kustomization.yaml still lists namespace.yaml; remove it."
    )


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
        # H1 — rendered output must declare a Namespace whose name
        # matches the overlay's ``namespace:`` field. The pure-YAML
        # test above asserts this from the source files; this test
        # double-checks it through the real kustomize renderer.
        target_ns = f"competitionops-{overlay}"
        rendered_docs = list(yaml.safe_load_all(result.stdout))
        ns_names = [
            d["metadata"]["name"]
            for d in rendered_docs
            if d and d.get("kind") == "Namespace"
        ]
        assert target_ns in ns_names, (
            f"overlay {overlay}: kustomize rendered Namespace names "
            f"{ns_names!r}, expected {target_ns!r}."
        )
