# Kubernetes deployment

Kustomize-managed manifests for shipping CompetitionOps Agent to a
cluster. Three overlays (dev, staging, prod) plus a hardened base.

## Layout

```
infra/k8s/
├── base/
│   ├── kustomization.yaml      lists all resources
│   ├── namespace.yaml
│   ├── deployment.yaml         nonroot + read-only-root + caps dropped
│   ├── service.yaml            ClusterIP, port 80 → container 8000
│   ├── configmap.yaml          non-secret env (AUDIT_LOG_DIR, OTEL_*)
│   ├── secret.template.yaml    7 keys, all empty — populated at deploy time
│   └── pvc.yaml                5Gi RWX claim for audit log
└── overlays/
    ├── dev/
    │   ├── kustomization.yaml
    │   └── deployment-patch.yaml   swaps PVC for emptyDir
    ├── staging/
    │   ├── kustomization.yaml
    │   └── ingress.yaml            letsencrypt-staging
    └── prod/
        ├── kustomization.yaml
        ├── deployment-patch.yaml   replicas=1 (H2-pinned) + podAntiAffinity
        └── ingress.yaml            letsencrypt-prod + rate-limit
```

## Hardening highlights

- **Distroless runtime**: ``gcr.io/distroless/python3-debian12:nonroot``
  has no shell, no pip, no apt — even a successful RCE has nowhere
  to go.
- **UID 65532 nonroot**: pod + container ``securityContext`` both pin
  uid; ``runAsNonRoot: true`` belt-and-suspenders.
- **Drop ALL capabilities**: container can't ``CAP_NET_RAW`` ping, can't
  ``CAP_SYS_PTRACE`` debug, etc.
- **Read-only root filesystem**: only ``/var/lib/competitionops/audit``
  (PVC) and ``/tmp`` (emptyDir, 64Mi cap) are writable.
- **No service account token automount**: eliminates the in-cluster
  token from the workload — the API doesn't talk to the K8s control
  plane.
- **seccompProfile=RuntimeDefault**: container locked to the kernel
  syscall allowlist the container runtime ships with.
- **Probes**: ``readinessProbe`` hits ``/health``, ``livenessProbe``
  hits ``/healthz`` — separation lets us drain a pod from the LB
  before kicking the runtime.

## Audit log persistence (Tier 0 #4)

The PVC ``competitionops-audit`` (5Gi, ReadWriteMany) backs
``AUDIT_LOG_DIR=/var/lib/competitionops/audit``. Each plan_id gets its
own ``<plan_id>.jsonl`` file (one line per ``AuditRecord``). The
``ReadWriteMany`` access mode was provisioned for the day H2 unblocks
multi-replica prod — for now prod is pinned to a single replica
(``replicas=1``) so the concurrent-append concern (deep-review H3) is
also dormant. The dev overlay swaps the PVC for an ``emptyDir`` so
minikube / kind / Docker Desktop work without an RWX storage class — at
the cost of losing audit on pod restart.

## H2 — single-replica prod gate

``src/competitionops/main.py::_plan_repo()`` defaults to
``InMemoryPlanRepository`` — a process-bound ``dict[str, ActionPlan]``.
With multiple pods on that adapter, a plan created on pod A is invisible
to pod B, so any ``POST /plans/{plan_id}/approve`` routed to a different
pod returns 404. The prod overlay is therefore pinned to ``replicas: 1``.

### What's already shipped

- ``FilePlanRepository`` adapter under
  ``src/competitionops/adapters/file_plan_store.py``. One JSON file per
  ``plan_id``, atomic-rename save (``os.replace``) so readers see either
  the old complete file or the new one, never a partial. Multi-pod
  readers on a shared volume are safe.
- Factory switch in ``_plan_repo()`` honors
  ``Settings.plan_repo_dir`` (env ``PLAN_REPO_DIR``), mirroring how
  ``_audit_log`` honors ``AUDIT_LOG_DIR``. Setting the env var alone
  flips both the FastAPI and the MCP processes onto the file-backed
  adapter.

### What still gates the pin

H3 — multi-writer safety for the ``FileAuditLog`` JSONL appends. While
replicas=1, only one pod writes the audit log, so the append-after-append
race is dormant. The moment replicas climbs without an H3 fix
(``fcntl.flock`` around the append, or per-pod filenames), concurrent
``approve_and_execute`` calls torn-write the JSONL and ``list_for_plan``
starts failing to parse some records.

### Operator checklist to lift the pin

1. Mount a shared volume at a path of your choice (the existing
   ``competitionops-audit`` PVC works; just pick a different subdir or a
   separate PVC for plans).
2. Set ``PLAN_REPO_DIR=/path/on/that/volume`` via configmap or secret.
3. Confirm H3 is closed in your build (until then, keep replicas=1).
4. Edit ``overlays/prod/deployment-patch.yaml`` to bump replicas. The
   ``podAntiAffinity`` block is already in place to spread pods.

## Secrets

``secret.template.yaml`` ships in the repo with seven empty fields.
**Never commit a real secret value.** Real values flow in via one of:

- **external-secrets.io** (recommended) — points at a cloud KMS / Vault
- **Sealed Secrets** — commit encrypted, decrypt in-cluster
- **kubectl create secret generic --from-literal=KEY=VAL** for staging

Settings's ``pydantic.SecretStr`` typing (Tier 0 #2) masks these in
``repr``, ``model_dump``, and ``model_dump_json``, so a misconfigured
logger can't leak them even if the Secret object ends up in stdout.

## Deploy

```bash
# Dev (minikube / kind / Docker Desktop)
kubectl apply -k infra/k8s/overlays/dev

# Staging
kubectl apply -k infra/k8s/overlays/staging

# Prod (with cert-manager + RWX storage installed)
kubectl apply -k infra/k8s/overlays/prod
```

## Tests

``tests/test_k8s_manifests.py`` parses every YAML directly (no
kustomize CLI required) and asserts the production posture above. 28
cases cover image, security context, probes, PVC mount path,
ConfigMap defaults, secret-template completeness, overlay
differentiation, ingress TLS + cluster-issuer, and the multi-stage
distroless Dockerfile. A 29th optional case runs
``kustomize build`` against each overlay — auto-skipped when the
``kustomize`` binary isn't on PATH.
