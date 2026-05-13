#!/usr/bin/env bash
set -euo pipefail

uv run uvicorn competitionops.main:app --reload
