# Multi-stage build: uv install into /app/deps, copy to distroless runtime.
#
# The distroless ``:nonroot`` runtime drops shells, package managers,
# and runs as UID 65532. Combined with the K8s Deployment's
# ``securityContext.runAsNonRoot: true`` this gives a hardened pod that
# cannot install packages, spawn shells, or escalate uid at runtime.

# ----- Stage 1: builder ------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /app

RUN pip install --no-cache-dir "uv>=0.11"

# Install dependencies into ``/app/deps`` rather than a venv so the
# distroless runtime (which has no shell or pip) can simply ``PYTHONPATH``
# at it. ``--extra otel`` pulls the production observability exporter
# package; flip it off if you want a slimmer image and accept that
# OTLP export silently no-ops.
#
# Round-2 M4 — ``INCLUDE_OCR`` build arg opts the image into Docling
# (real layout-aware PDF extraction). Default build leaves it empty,
# so the image stays slim. Operators flipping ``PDF_ADAPTER=docling``
# at runtime MUST also build with this arg or the runtime factory
# raises ``RuntimeError`` at the first PDF upload.
#
# Round-3 M3 — image tag must match the kustomize overlay's
# ``newName: competitionops/api`` so ``kubectl apply -k overlays/<env>``
# resolves the image without an extra ``kustomize edit set image``.
# Slim build:
#
#   docker build -t competitionops/api:<env> -f infra/docker/Dockerfile .
#
# OCR build (Docling enabled):
#
#   docker build --build-arg INCLUDE_OCR=1 \
#       -t competitionops/api:<env>-ocr \
#       -f infra/docker/Dockerfile .
#
# The ``${INCLUDE_OCR:+--extra ocr}`` shell expansion adds the flag
# only when ``INCLUDE_OCR`` is non-empty; default = slim image.
#
# Round-4 Medium#7 — ``INCLUDE_WEB`` is the symmetric build arg for
# P1-006 Crawl4AI web ingestion. ``--build-arg INCLUDE_WEB=1`` installs
# the ``web`` extra (Crawl4AI + Playwright Python packages). NOTE: this
# installs the Python packages only — the Chromium browser binary is a
# separate concern. The distroless runtime cannot host
# ``playwright install`` (no shell, no apt), so operators provision the
# browser at runtime via an emptyDir + ``PLAYWRIGHT_BROWSERS_PATH``
# (infra/k8s/README.md "Enabling Crawl4AI", Option B). Without browser
# provisioning, ``WEB_ADAPTER=crawl4ai`` fails gracefully at the first
# fetch (caught RuntimeError), not at import.
ARG INCLUDE_OCR=""
ARG INCLUDE_WEB=""
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv pip install --no-cache --target /app/deps --extra otel \
    ${INCLUDE_OCR:+--extra ocr} ${INCLUDE_WEB:+--extra web} .

# ----- Stage 2: distroless runtime -------------------------------------------
FROM gcr.io/distroless/python3-debian12:nonroot

WORKDIR /app

COPY --from=builder --chown=nonroot:nonroot /app/deps /app/deps
COPY --from=builder --chown=nonroot:nonroot /app/src /app/src

ENV PYTHONPATH="/app/deps:/app/src" \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# distroless image's default entrypoint is /usr/bin/python3, but pinning
# explicitly avoids surprises when a base-image update changes that.
ENTRYPOINT ["python", "-m", "uvicorn", \
            "competitionops.main:app", \
            "--host", "0.0.0.0", \
            "--port", "8000"]
