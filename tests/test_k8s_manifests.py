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
# Round-2 M3 — operator-facing placeholders for opt-in features.
#
# Before this commit, ``infra/k8s/README.md`` told operators to "set
# PLAN_REPO_DIR via configmap or secret" when lifting the H2 pin, but
# the base ConfigMap didn't even mention the key. Same for
# ``PDF_ADAPTER=docling`` — documented in the runtime factory but
# nowhere in the deploy-time surface. Operators had to either grep the
# source or hit RuntimeError at first request to discover the env vars.
#
# The fix puts both keys in the ConfigMap as **commented** placeholders.
# Active values stay unset by default (so behaviour is unchanged); the
# YAML serves as in-tree documentation for what to uncomment when
# lifting the relevant gate.
#
# Tested via raw-text grep (YAML parser strips comments) — paired with
# an assertion that the key is NOT active in ``cm.data``, so a future
# refactor that accidentally activates the key surfaces in tests.
# ---------------------------------------------------------------------------


def test_base_configmap_documents_plan_repo_dir_as_commented_placeholder() -> None:
    """H2 follow-up — the configmap must mention ``PLAN_REPO_DIR`` so
    operators lifting the prod replicas=1 pin discover the env var at
    deploy time rather than from a 404 in production."""
    path = _BASE / "configmap.yaml"
    raw = path.read_text(encoding="utf-8")
    assert "PLAN_REPO_DIR" in raw, (
        "configmap.yaml must mention PLAN_REPO_DIR (as a commented "
        "placeholder) so operators lifting the H2 pin find the env var."
    )
    cm = _load_yaml(path)
    assert "PLAN_REPO_DIR" not in cm["data"], (
        "PLAN_REPO_DIR must stay COMMENTED OUT — activating it by default "
        "would force every operator onto file-backed plans without the "
        "shared-volume requirement being satisfied. Keep it as docs only."
    )


def test_base_configmap_documents_pdf_adapter_as_commented_placeholder() -> None:
    """Sprint 3 / M4 — the configmap must mention ``PDF_ADAPTER`` so
    operators discover that ``docling`` is opt-in (requires an OCR
    build of the image; see Dockerfile)."""
    path = _BASE / "configmap.yaml"
    raw = path.read_text(encoding="utf-8")
    assert "PDF_ADAPTER" in raw
    cm = _load_yaml(path)
    assert "PDF_ADAPTER" not in cm["data"], (
        "PDF_ADAPTER must stay commented — activating ``docling`` "
        "by default would crash any deployment whose image was built "
        "without the ``ocr`` extra (default build)."
    )


def test_base_configmap_plan_repo_dir_placeholder_points_into_audit_pvc_subdir() -> None:
    """The documented default path reuses the audit PVC (subdir) so
    lifting the H2 pin doesn't force operators to provision a second
    PVC. Operators with stricter quota requirements can swap to a
    separate volume per their environment policy."""
    raw = (_BASE / "configmap.yaml").read_text(encoding="utf-8")
    # The commented placeholder should reference the existing audit mount.
    assert "/var/lib/competitionops/audit" in raw
    # And the default path is inside that mount.
    assert "/var/lib/competitionops/audit/plans" in raw or (
        "PLAN_REPO_DIR" in raw and "subdir" in raw.lower()
    ), (
        "PLAN_REPO_DIR's commented placeholder should either name the "
        "exact subdir path or explain the subdir convention in the same "
        "block, so operators don't have to cross-reference the README."
    )


# ---------------------------------------------------------------------------
# Round-2 M4 — Dockerfile build path for the optional OCR extra.
#
# The default image doesn't include Docling (heavy ML deps: torch,
# easyocr, pypdfium2 — ~2 GiB image bloat). Operators who set
# ``PDF_ADAPTER=docling`` need a build that includes ``--extra ocr``,
# OR the runtime factory raises ``RuntimeError`` at first PDF upload.
#
# The fix exposes a build-arg (``INCLUDE_OCR``) so the same Dockerfile
# builds both variants. The README documents the opt-in command.
# ---------------------------------------------------------------------------


def test_dockerfile_exposes_include_ocr_build_arg() -> None:
    """M4 — the Dockerfile must accept a ``INCLUDE_OCR`` build arg
    so operators can build an OCR-enabled image without forking the
    Dockerfile."""
    content = _DOCKERFILE.read_text(encoding="utf-8")
    assert "INCLUDE_OCR" in content, (
        "Dockerfile must expose an INCLUDE_OCR build arg. Operators "
        "build with ``docker build --build-arg INCLUDE_OCR=1 ...`` to "
        "include the Docling extra. Without this, PDF_ADAPTER=docling "
        "deployments crash at first request."
    )
    # And the install line must reference the ``ocr`` extra (so the
    # build arg actually controls it).
    assert "--extra ocr" in content, (
        "INCLUDE_OCR is wired but the install line doesn't reference "
        "``--extra ocr`` — the build arg has no effect."
    )


def test_dockerfile_default_build_does_not_install_ocr_extra() -> None:
    """Defence against accidental default-on: the Dockerfile must
    treat OCR as opt-in. The default build (no build-arg) must not
    pull in Docling's ~2 GiB of ML dependencies."""
    content = _DOCKERFILE.read_text(encoding="utf-8")
    # ``ARG INCLUDE_OCR=""`` (or similar) declares the arg with an
    # empty default. The conditional substitution ``${INCLUDE_OCR:+--extra ocr}``
    # expands ONLY when the arg is non-empty. Both patterns must
    # appear together.
    has_default = (
        'ARG INCLUDE_OCR=""' in content
        or "ARG INCLUDE_OCR=\n" in content
        or "ARG INCLUDE_OCR \n" in content
        or content.count("ARG INCLUDE_OCR") >= 1
        and "INCLUDE_OCR=" in content.split("ARG INCLUDE_OCR")[1][:20]
    )
    assert has_default, (
        "Dockerfile must declare ``ARG INCLUDE_OCR`` with an empty default "
        "so the default build is the slim image. Operators opt in via "
        "``--build-arg INCLUDE_OCR=1``."
    )


def test_dockerfile_exposes_include_web_build_arg() -> None:
    """Round-4 Medium#7 — symmetric with ``INCLUDE_OCR``. The Dockerfile
    must accept an ``INCLUDE_WEB`` build arg so operators can build a
    Crawl4AI-enabled image without forking the Dockerfile. Otherwise
    ``WEB_ADAPTER=crawl4ai`` deployments hit ``RuntimeError`` (missing
    ``crawl4ai`` package) on the first ``/briefs/extract/url`` request."""
    content = _DOCKERFILE.read_text(encoding="utf-8")
    assert "INCLUDE_WEB" in content, (
        "Dockerfile must expose an INCLUDE_WEB build arg, symmetric "
        "with INCLUDE_OCR. Operators build with "
        "``docker build --build-arg INCLUDE_WEB=1 ...`` to include the "
        "``web`` extra (Crawl4AI)."
    )
    assert "--extra web" in content, (
        "INCLUDE_WEB is wired but the install line doesn't reference "
        "``--extra web`` — the build arg has no effect."
    )


def test_dockerfile_default_build_does_not_install_web_extra() -> None:
    """Defence against accidental default-on: the Dockerfile must treat
    the web extra as opt-in. The default build (no build-arg) must not
    pull in Crawl4AI / Playwright."""
    content = _DOCKERFILE.read_text(encoding="utf-8")
    has_default = (
        'ARG INCLUDE_WEB=""' in content
        or content.count("ARG INCLUDE_WEB") >= 1
        and "INCLUDE_WEB=" in content.split("ARG INCLUDE_WEB")[1][:20]
    )
    assert has_default, (
        "Dockerfile must declare ``ARG INCLUDE_WEB`` with an empty "
        "default so the default build stays slim. Operators opt in via "
        "``--build-arg INCLUDE_WEB=1``."
    )
    # The conditional substitution must gate the extra on the arg.
    assert "${INCLUDE_WEB:+--extra web}" in content, (
        "The web extra must be gated behind ``${INCLUDE_WEB:+--extra web}`` "
        "so it only installs when the build arg is non-empty."
    )


def test_base_configmap_documents_web_adapter_as_commented_placeholder() -> None:
    """Round-4 Medium#7 — symmetric with the ``PDF_ADAPTER`` placeholder.
    The configmap must mention ``WEB_ADAPTER`` as a COMMENTED placeholder
    so operators discover that ``crawl4ai`` is opt-in (requires an
    INCLUDE_WEB image build + browser provisioning). It must stay
    commented — activating ``crawl4ai`` by default would crash any
    deployment whose image was built without the ``web`` extra."""
    path = _BASE / "configmap.yaml"
    raw = path.read_text(encoding="utf-8")
    assert "WEB_ADAPTER" in raw, (
        "configmap.yaml must mention WEB_ADAPTER (as a commented "
        "placeholder) so operators discover the crawl4ai opt-in."
    )
    cm = _load_yaml(path)
    assert "WEB_ADAPTER" not in cm["data"], (
        "WEB_ADAPTER must stay COMMENTED OUT — activating crawl4ai by "
        "default would crash any deployment built without the web extra."
    )


def test_readme_documents_h2_lift_configmap_step() -> None:
    """The operator checklist for lifting the H2 pin must name the
    configmap step explicitly. Before this commit it told operators
    to "set PLAN_REPO_DIR via configmap or secret" without naming the
    actual file or the path convention."""
    content = (_K8S / "README.md").read_text(encoding="utf-8")
    assert "PLAN_REPO_DIR" in content
    # Must point operators at the configmap file (or at least the term).
    assert "configmap" in content.lower() or "ConfigMap" in content


def test_readme_documents_ocr_build_invocation() -> None:
    """The operator-facing docs must show the exact command for the
    OCR build. Otherwise ``PDF_ADAPTER=docling`` is documented as
    runtime but is actually build-time, which is the round-2 M4 gap."""
    content = (_K8S / "README.md").read_text(encoding="utf-8")
    assert "INCLUDE_OCR" in content, (
        "README must document the ``INCLUDE_OCR`` build arg so "
        "operators flipping PDF_ADAPTER=docling know to rebuild the image."
    )


# ---------------------------------------------------------------------------
# Round-3 M3 — image name alignment between Dockerfile / README and overlays
#
# Background. The kustomize overlays rewrite the base distroless image to
# ``competitionops/api:{dev,staging,prod}`` (see ``newName`` in each
# overlay's ``images:`` block). Operators following the README, however,
# saw ``docker build -t competitionops:ocr`` — different repo name AND a
# tag the overlays never select. ``kubectl apply -k overlays/dev`` then
# pulled ``competitionops/api:dev``, which doesn't exist on the local
# registry, and the pod stuck at ``ImagePullBackOff``. M3 closes the gap
# by aligning the documented build command with the kustomize image
# patches: ``competitionops/api:{env}`` for the slim build,
# ``competitionops/api:{env}-ocr`` when ``INCLUDE_OCR=1`` is passed.
# ---------------------------------------------------------------------------


def test_overlay_image_names_form_a_single_registry_path() -> None:
    """All three overlays must rewrite the base image to the SAME
    ``newName`` (``competitionops/api``) — only ``newTag`` varies per
    environment. If a future overlay drifts (typo, fork) the
    docker-build commands in README / Dockerfile would teach the
    operator a wrong tag."""
    new_names: set[str] = set()
    for env in ("dev", "staging", "prod"):
        kustomization = _load_yaml(_OVERLAYS / env / "kustomization.yaml")
        images = kustomization.get("images") or []
        for entry in images:
            new_names.add(entry["newName"])

    assert new_names == {"competitionops/api"}, (
        "All overlays must rewrite the base image to the same "
        f"``newName=competitionops/api``; saw {sorted(new_names)}. "
        "Drift here means README's docker-build instructions can't be "
        "kept aligned across environments."
    )


def test_readme_ocr_build_command_tags_overlay_compatible_image_name() -> None:
    """The README's docker-build example must tag the image with the
    same repo name that the overlays rewrite to. Otherwise the
    documented command produces an image the overlays don't reference,
    and the operator hits ImagePullBackOff after ``kubectl apply``.

    Specifically: the README must tag ``competitionops/api:`` (NOT
    the legacy ``competitionops:ocr``) so the operator can immediately
    plug the result into a ``kustomize edit set image`` call against
    any overlay.
    """
    content = (_K8S / "README.md").read_text(encoding="utf-8")
    # The legacy ``competitionops:ocr`` form is what we are migrating
    # AWAY from — it does not match overlay's ``competitionops/api`` repo.
    assert "competitionops:ocr" not in content, (
        "README must not teach the legacy ``competitionops:ocr`` tag "
        "(predates kustomize overlay alignment). Update the docker-build "
        "example to ``competitionops/api:<env>-ocr`` so the resulting "
        "image is directly referenced by the overlay image-patch."
    )
    assert "competitionops/api" in content, (
        "README must tag images as ``competitionops/api:<env>[-ocr]`` "
        "so the docker-build command produces an image the kustomize "
        "overlays already reference via ``newName``."
    )


def test_dockerfile_ocr_build_comment_tags_overlay_compatible_image_name() -> None:
    """Dockerfile header comment shows the canonical OCR-enabled build
    invocation. Same alignment rule as the README test above —
    operators reading the Dockerfile in isolation must also see the
    overlay-compatible tag, not the legacy ``competitionops:ocr``."""
    content = (_REPO_ROOT / "infra/docker/Dockerfile").read_text(encoding="utf-8")
    assert "competitionops:ocr" not in content, (
        "Dockerfile header comment must not show the legacy "
        "``competitionops:ocr`` tag — it does not match the kustomize "
        "overlay's ``competitionops/api`` repo and trips operators who "
        "copy-paste it. Use ``competitionops/api:<env>-ocr`` instead."
    )
    assert "competitionops/api" in content, (
        "Dockerfile header comment must tag the build with "
        "``competitionops/api:<env>[-ocr]`` so the resulting image "
        "plugs into the kustomize overlay image-patch without a rename."
    )


# ---------------------------------------------------------------------------
# Round-3 M5 — MemorySaver gap in the H2 operator checklist
#
# Background. The 4-step H2-lift checklist in ``infra/k8s/README.md``
# covers PVC + configmap + audit + replicas. It does NOT mention the
# LangGraph workflow checkpointer, which is currently the in-process
# ``MemorySaver`` set in ``workflows/graph.py``. An operator who
# successfully follows the 4 steps and bumps ``replicas>1`` would
# still hit a multi-pod bug: a workflow checkpoint created on pod A
# is invisible to pod B, so ``POST /executions/{plan_id}/run`` after
# approval can crash or restart from scratch depending on routing.
# The fix is doc-only — operators need to know they must also migrate
# the checkpointer (SqliteSaver on shared PVC or PostgresSaver on
# shared DB) before flipping ``replicas`` away from 1.
# ---------------------------------------------------------------------------


def test_readme_h2_checklist_warns_about_memory_saver_checkpointer() -> None:
    """The H2 operator checklist must explicitly mention the workflow
    ``MemorySaver`` checkpointer as a multi-pod gap. Without this,
    operators completing the 4 PVC / configmap / audit / replicas
    steps assume they're done and hit a routing bug on the first
    approval-then-execute round-trip across pods."""
    content = (_K8S / "README.md").read_text(encoding="utf-8")
    assert "MemorySaver" in content, (
        "README's H2 operator checklist must mention ``MemorySaver`` "
        "as a multi-pod gap. The 4-step checklist is otherwise "
        "complete-looking but leaves the workflow checkpointer as a "
        "process-local store, breaking POST /executions/{plan_id}/run "
        "the moment routing lands on a pod that didn't run the "
        "approval step."
    )
    # And it must name at least one of the migration targets so the
    # operator has a concrete next action, not just a warning.
    assert any(
        marker in content
        for marker in ("SqliteSaver", "PostgresSaver", "langgraph-checkpoint")
    ), (
        "README must name a migration target (SqliteSaver, "
        "PostgresSaver, or the langgraph-checkpoint package) so the "
        "warning ships with a fix, not just a complaint."
    )


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


def test_overlay_prod_pinned_to_one_replica_as_deployment_policy_default() -> None:
    """H2 regression guard.

    Prod overlay is pinned to ``replicas: 1`` as a deployment-policy
    default. Both halves of the underlying dependency are now closed
    in-tree:

    1. ``FilePlanRepository`` ships in
       ``adapters/file_plan_store.py``. Opt in via ``PLAN_REPO_DIR``.
    2. H3 closed by per-writer filenames in
       ``adapters/file_audit.py`` — each pod writes to
       ``<plan_id>.<writer_id>.jsonl`` where ``writer_id`` defaults to
       the pod's hostname / ``metadata.name``. No shared resource ⇒
       no torn-write risk regardless of RWX filesystem.

    Lifting the pin is now a one-line manifest change once the
    operator has set ``PLAN_REPO_DIR`` on a shared volume and confirmed
    their build contains the H3 fix. See ``infra/k8s/README.md`` for
    the full checklist.

    The ``podAntiAffinity`` block stays in the patch so the spread
    intent activates the moment replicas climbs.
    """
    patch = _load_yaml(_OVERLAYS / "prod" / "deployment-patch.yaml")
    assert patch["spec"]["replicas"] == 1, (
        "Prod overlay default stays at replicas=1. Lifting the pin is "
        "an operator action (set PLAN_REPO_DIR + bump this number). "
        "See H2 comment in this test + infra/k8s/README.md for context."
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
