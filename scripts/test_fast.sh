#!/usr/bin/env bash
# Full test run of auth-service on a developer machine: pytest-xdist, one database per worker.
#
#   scripts/test_fast.sh                     whole suite, up to 4 workers
#   scripts/test_fast.sh tests/test_x.py -k name    the same switches as pytest
#   WORKERS=2 scripts/test_fast.sh           fewer workers (a 16 GB Mac heats up above 4; the script never goes above 4)
#
# Needs pytest-xdist (pip install pytest-xdist==3.8.0). Without A2_AUTH_TEST_DATABASE_URL the script starts a throwaway
# postgres:15-alpine container (fsync off, removed on exit) and sets all the test variables itself. With the variables set it uses YOUR server:
# worker gwK works in <database>_gwK (tests/conftest.py creates it), and the script drops those copies at the end (KEEP_DBS=1 keeps them).
# Whole test files go to one worker (--dist loadfile), so the order of tests inside a file is the same as in CI.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON=${PYTHON:-python3}
"$PYTHON" -c 'import xdist' 2>/dev/null || { echo "pytest-xdist is missing: $PYTHON -m pip install pytest-xdist==3.8.0" >&2; exit 2; }

CPUS=$("$PYTHON" -c 'import os; print(os.cpu_count() or 1)')
WORKERS=${WORKERS:-4}
[ "$WORKERS" -gt 4 ] && WORKERS=4
[ "$WORKERS" -gt "$CPUS" ] && WORKERS=$CPUS
[ "$WORKERS" -lt 1 ] && WORKERS=1

CONTAINER=""
cleanup() {
  if [ -n "$CONTAINER" ]; then
    docker rm -fv "$CONTAINER" >/dev/null 2>&1 || true
  elif [ "${KEEP_DBS:-0}" != 1 ] && [ -n "${A2_AUTH_TEST_DATABASE_URL:-}" ]; then
    "$PYTHON" scripts/drop_worker_databases.py "$A2_AUTH_TEST_DATABASE_URL" "$A5_AUTH_MIGRATION_TEST_DATABASE_URL" || true
  fi
}
trap cleanup EXIT

if [ -z "${A2_AUTH_TEST_DATABASE_URL:-}" ]; then
  command -v docker >/dev/null || { echo "set A2_AUTH_TEST_DATABASE_URL / A5_AUTH_MIGRATION_TEST_DATABASE_URL or install docker" >&2; exit 2; }
  CONTAINER="algo-auth-test-fast-$$"
  docker run -d --name "$CONTAINER" -e POSTGRES_USER=algo -e POSTGRES_PASSWORD=ci-tests-only -e POSTGRES_DB=a2_auth \
    -p 127.0.0.1::5432 postgres:15-alpine -c fsync=off -c synchronous_commit=off -c full_page_writes=off -c max_connections=200 >/dev/null
  PORT=$(docker port "$CONTAINER" 5432/tcp | head -1 | sed 's/.*://')
  # pg_isready also answers on the temporary init server (which then restarts), so ask with a real query, over TCP, with a time limit
  READY=0
  for _ in $(seq 1 120); do
    if docker exec "$CONTAINER" psql -h 127.0.0.1 -U algo -d a2_auth -tAc "SELECT 1" >/dev/null 2>&1; then READY=1; break; fi
    sleep 0.5
  done
  [ "$READY" = 1 ] || { echo "postgres did not become ready in 60 s" >&2; exit 2; }
  docker exec "$CONTAINER" psql -U algo -d postgres -qc "CREATE DATABASE a5_auth_migrations" >/dev/null
  BASE="postgresql+psycopg2://algo:ci-tests-only@127.0.0.1:$PORT"
  export A2_AUTH_TEST_DATABASE_URL="$BASE/a2_auth" A5_AUTH_MIGRATION_TEST_DATABASE_URL="$BASE/a5_auth_migrations" DATABASE_URL="$BASE/a2_auth"
fi
: "${A5_AUTH_MIGRATION_TEST_DATABASE_URL:?set A5_AUTH_MIGRATION_TEST_DATABASE_URL (the .../a5_auth_migrations database) too}"

export APP_ENV=${APP_ENV:-testing}
export SECRET_KEY=${SECRET_KEY:-ci-tests-only-secret-at-least-32-characters}
export DATABASE_URL=${DATABASE_URL:-$A2_AUTH_TEST_DATABASE_URL}
export REDIS_URL=${REDIS_URL:-redis://localhost:6379/0}      # tests never reach Redis (tests/conftest.py)
export INTERNAL_SERVICE_TOKEN=${INTERNAL_SERVICE_TOKEN:-ci-tests-only-internal-token}
export GEMINI_API_KEY=${GEMINI_API_KEY-}

echo "workers: $WORKERS (cpus $CPUS)"
"$PYTHON" -m pytest -n "$WORKERS" --dist loadfile -q --tb=short -p no:cacheprovider "$@"
