#!/usr/bin/env bash
#
# Boot a StemDeck backend for the browser tests, against a throwaway jobs
# directory seeded with one finished track.
#
# Everything the app writes is redirected into that directory, so a test run can
# never read, modify or delete a developer's real library. The directory is
# recreated on every run, so state cannot leak between runs either.

set -euo pipefail

PORT="${1:-8123}"
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK_DIR="${STEMDECK_E2E_DIR:-${TMPDIR:-/tmp}/stemdeck-e2e}"

rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR/jobs" "$WORK_DIR/data"

uv run python "${REPO_ROOT}/tests/e2e/seed.py" "$WORK_DIR/jobs" >/dev/null

cd "$REPO_ROOT"
exec env \
    STEMDECK_JOBS_DIR="$WORK_DIR/jobs" \
    STEMDECK_DATA_DIR="$WORK_DIR/data" \
    STEMDECK_CACHE_DIR="$WORK_DIR/data/cache" \
    STEMDECK_LOGS_DIR="$WORK_DIR/data/logs" \
    STEMDECK_MODELS_DIR="$WORK_DIR/data/models" \
    STEMDECK_DOWNLOADS_DIR="$WORK_DIR/data/downloads" \
    uv run uvicorn app.main:app \
        --host 127.0.0.1 \
        --port "$PORT" \
        --log-level warning \
        --timeout-graceful-shutdown 2
