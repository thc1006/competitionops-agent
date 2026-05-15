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

### What still shipped — H3 closed

H3 — multi-writer safety for ``FileAuditLog`` — is now closed in-tree
by per-writer filenames rather than ``fcntl.flock``. Layout::

    <base_dir>/
      ├── <plan_id>.<pod_hostname_a>.jsonl   # written by pod a only
      ├── <plan_id>.<pod_hostname_b>.jsonl   # written by pod b only
      └── ...

``writer_id`` defaults to ``socket.gethostname()`` which in a k8s pod
is the pod's ``metadata.name`` — no extra wiring required. Each pod
owns its own file, so there is no shared resource between writers
and torn writes are impossible regardless of the underlying
filesystem (NFS, CephFS, Azure Files, EFS, …). ``list_for_plan``
globs ``<plan_id>.*.jsonl`` and merges across writers; legacy
``<plan_id>.jsonl`` files from pre-H3 deployments are also picked up
so in-place upgrades keep the historical audit trail.

### Operator checklist to lift the pin

The four steps below assume your build is on top of the H3 fix
(``adapters/file_audit.py`` writes ``<plan_id>.<writer_id>.jsonl``).
Round-2 audit confirmed this is the only remaining gate.

1. **Pick a shared volume for plans.** The existing
   ``competitionops-audit`` PVC works as-is — plans go into a subdir
   (``/var/lib/competitionops/audit/plans``), no second PVC, no second
   ``volumeMount``. Operators with stricter quota isolation between
   audit and plan data can provision a separate PVC + mount at a
   distinct path.
2. **Uncomment ``PLAN_REPO_DIR`` in** ``infra/k8s/base/configmap.yaml``.
   The default value (subdir of the audit mount) is pre-written;
   change the path only if step 1 picked a separate PVC.
3. **Confirm H3 is in your build.** The audit-log layout under
   ``competitionops-audit/`` should contain
   ``<plan_id>.<pod-name>.jsonl`` files (one per pod). If you still
   see ``<plan_id>.jsonl`` only, your image is pre-H3 — rebuild.
4. **Migrate the workflow checkpointer off ``MemorySaver``.** The
   LangGraph workflow in ``src/competitionops/workflows/graph.py``
   currently defaults to an in-process ``MemorySaver`` checkpointer.
   That's correct for single-replica but becomes a hidden multi-pod
   bug once ``replicas>1``: a plan whose ``plan_node`` ran on pod A
   has its checkpoint in pod A's memory. ``POST /executions/{plan_id}/run``
   after approval routes by L7 hash to **any** pod, and pod B has no
   ``thread_id`` for that plan — the workflow either crashes on
   resume or silently restarts from scratch.

   This step is a **code change**, not a deploy-time toggle —
   merge it into a release before bumping replicas. Pick one
   migration target:

   - ``langgraph.checkpoint.sqlite.SqliteSaver`` (PyPI:
     ``langgraph-checkpoint-sqlite``) against a shared sqlite file on
     the audit PVC (RWX). Single file, simple, good for low-throughput.
   - ``langgraph.checkpoint.postgres.PostgresSaver`` (PyPI:
     ``langgraph-checkpoint-postgres``) against the same PostgreSQL
     Sprint 7 will provision for plan persistence. Better concurrency,
     requires a schema migration. Neither saver ships in
     ``pyproject.toml`` today — add the chosen one as an optional
     extra (mirroring ``[ocr]``) in the same PR that does the wiring.

   Wire the chosen saver into ``build_graph(checkpointer=...)`` and,
   when implemented, surface the choice via a Settings field /
   env var (e.g. ``LANGGRAPH_CHECKPOINTER=sqlite``; this field
   does not exist yet — Sprint 7 work).
5. **Bump replicas in** ``overlays/prod/deployment-patch.yaml``. The
   ``podAntiAffinity`` block is already in place to spread pods
   across nodes; un-pin ``replicas: 1`` once the above is done.

## Enabling Docling (real PDF extraction)

P2-005 Sprint 3 ships ``DoclingPdfAdapter`` for layout-aware PDF
parsing. The default image does NOT include Docling because its
transitive deps (``torch``, ``easyocr``, ``pypdfium2``,
``huggingface-hub``) add ~2 GiB to the image. Operators opt in at
**build time** via a build-arg:

```
docker build --build-arg INCLUDE_OCR=1 \
    -t competitionops/api:dev-ocr \
    -f infra/docker/Dockerfile .
```

The image name must match the kustomize overlay's ``newName:
competitionops/api`` (see ``infra/k8s/overlays/<env>/kustomization.yaml``).
Use ``-ocr`` as the tag suffix when ``INCLUDE_OCR=1`` is set so the
slim and OCR builds can coexist in a registry; e.g. for staging use
``-t competitionops/api:staging-ocr`` and update the overlay's
``newTag`` (or pass ``--load`` directly into a kind / minikube cluster
for dev). Round-3 M3 closed an alignment gap where the README previously taught
a legacy single-name tag that never matched the overlay's
``newName`` and silently produced ``ImagePullBackOff``.

Then at runtime, set ``PDF_ADAPTER=docling`` in the configmap (the
commented placeholder is in ``infra/k8s/base/configmap.yaml``). The
runtime factory verifies the package is importable; mismatched
config (``PDF_ADAPTER=docling`` against a slim image) surfaces as a
clear ``RuntimeError`` at the first PDF upload pointing at
``--build-arg INCLUDE_OCR=1``.

## Enabling Crawl4AI (real web ingestion) — egress restriction is mandatory

P1-006 Sprint 2 ships ``Crawl4AIWebAdapter`` for ``POST /briefs/extract/url``.
Activate by setting ``WEB_ADAPTER=crawl4ai`` in the configmap AND
including the ``[web]`` extra at install time (``uv sync --extra web``).
Crawl4AI ships ~200MB of Playwright + Chromium; operators who don't
need real web ingestion should leave ``WEB_ADAPTER`` unset (mock
adapter, no install footprint).

**SSRF defence requires BOTH layers:**

1. **Pydantic validator** (Sprint 1, in-process) — resolves the URL's
   hostname once and rejects on banned IP ranges (loopback / RFC-1918 /
   link-local / IPv6 ULA / 169.254.169.254 cloud metadata / reserved /
   multicast / unspecified). See ``main._validate_url_safety``.

2. **NetworkPolicy** (this section, cluster-level) — closes the DNS
   rebinding gap. Crawl4AI's Playwright backend re-resolves DNS at
   connect time; a malicious authoritative server can return a public
   IP to the validator and a private IP to the browser. The validator
   ALONE cannot stop that.

Minimum NetworkPolicy for the API pod when ``WEB_ADAPTER=crawl4ai``:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: competitionops-api-egress
  namespace: competitionops
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: competitionops
      app.kubernetes.io/component: api
  policyTypes: [Egress]
  egress:
    - to:
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8         # RFC1918
              - 172.16.0.0/12
              - 192.168.0.0/16
              - 169.254.0.0/16     # link-local incl. cloud metadata
              - 127.0.0.0/8        # loopback (cluster shouldn't route here anyway)
              - 100.64.0.0/10      # CGNAT (k8s pod / service CIDRs often overlap)
    # Plus DNS egress to cluster DNS so Crawl4AI's browser can resolve.
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
          podSelector:
            matchLabels:
              k8s-app: kube-dns
      ports:
        - protocol: UDP
          port: 53
```

Adjust the ``except`` list to your cluster's network plan (e.g., add
``172.16.0.0/12`` only if it's actually internal; remove if pod CIDR
lives there). The default above is conservative for a typical
cloud-hosted cluster.

Without this policy, the validator's resolve-once check is
defeatable by DNS rebinding — Sprint 2's adapter has no way to
re-validate at connect time. Treat the NetworkPolicy as part of the
deploy contract: ``WEB_ADAPTER=crawl4ai`` without it is a security
regression, not a configuration choice.

### Browser cache vs ``readOnlyRootFilesystem: true``

The base ``deployment.yaml`` hardening sets ``readOnlyRootFilesystem: true``
(see "Hardening highlights" above). Playwright (under Crawl4AI) writes
browser binaries + per-session state to ``~/.cache/ms-playwright/`` by
default — a path on the read-only root that the runtime user
(``runAsUser: 65532``) can't create. The first ``/briefs/extract/url``
request fails with ``EROFS: read-only file system`` mid-fetch.

Two ways to close this:

**Option A (recommended) — bake the browser into the image at build time.**
The default ``infra/docker/Dockerfile`` does not run ``playwright install``;
extend it (or maintain a separate ``Dockerfile.web``) with a stage that
runs ``playwright install --with-deps chromium`` AFTER the
``--extra web`` install, writing into a writable image layer. The
result is a self-contained image — no writable mount needed at runtime.

**Option B — runtime writable cache via emptyDir.** Add a sized
emptyDir mount + point Playwright at it via env var:

```yaml
spec:
  template:
    spec:
      containers:
        - name: api
          env:
            - name: PLAYWRIGHT_BROWSERS_PATH
              value: /var/cache/playwright
          volumeMounts:
            - name: playwright-cache
              mountPath: /var/cache/playwright
      volumes:
        - name: playwright-cache
          emptyDir:
            sizeLimit: 512Mi   # Chromium + dependencies ≈ 300MB
```

Chromium binaries pre-download on first fetch (~30s cold start), then
sit in the emptyDir for the pod's lifetime. Each pod restart re-downloads
— acceptable for low-volume web ingestion. Operators running high
throughput should prefer Option A.

Either option must be in place BEFORE flipping ``WEB_ADAPTER=crawl4ai``.
The base manifests deliberately stay minimal (no Playwright wiring) so
operators who don't need real web ingestion don't pay the storage cost.

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
