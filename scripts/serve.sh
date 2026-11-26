#!/usr/bin/env bash
# Reseed the database and start the API for a load-test run.
#   scripts/serve.sh                 # every fix in place
#   PERF_BUGS=n_plus_one scripts/serve.sh
set -euo pipefail
export RATE_LIMIT_PER_MINUTE="${RATE_LIMIT_PER_MINUTE:-0}"
"${PYTHON:-python}" -m app.seed --events "${EVENTS:-200}" --seats "${SEATS:-500}" --users "${USERS:-200}"
exec "${PYTHON:-python}" -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1 --log-level warning
