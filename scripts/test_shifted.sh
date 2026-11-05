#!/usr/bin/env bash
# Date-bomb finder (LESSONS 76/82/102): the whole suite with the REAL clock moved ahead, in Docker. Not a CI job - a manual check before a release that touches time, and a periodic sweep.
#
#   FAKE_SHIFT=+30d bash scripts/test_shifted.sh          (also +1d +7d +60d +400d +18h, an absolute UTC moment 'YYYY-MM-DD HH:MM', or pytest switches after it: ... tests/test_x.py -k name)
#
# Postgres and pytest run in two throwaway containers whose wall clock (gettimeofday / clock_gettime REALTIME / time) is moved by the same offset with a small LD_PRELOAD shim built into the images, so
# ``now()``, ``server_default`` stamps, ``datetime.utcnow()`` and ``date.today()`` all move; the machine's own clock and databases are untouched. A test that pins the application clock to a fixed day
# and compares it with a real stamp is red at some shift - that is the bomb. NOT libfaketime (measured 05.11): it stops the timers of postgres (``lock_timeout``, ``statement_timeout``) at any non-zero shift
# and, even at +0, breaks the threads of a dozen concurrency tests - artifacts, not bombs. To tell a bomb from a flake, repeat the failed ids at ``+1s``: one second can not create a date bomb.
#
# It can not hang silently: a test fails after TEST_TIMEOUT s (pytest-timeout ``signal``; ``thread`` would make xdist re-run the whole file after each kill), a statement after PG_STATEMENT_TIMEOUT
# (a call blocked in libpq is not interrupted by a signal), the whole run is killed after RUN_LIMIT_MIN with a message, and the containers and the network are removed at any exit (Ctrl-C too).
# The 5000-row Excel test (test_opening_bulk_r3_pg) takes ~90 s alone: on a busy machine it hits the 120 s limit - use TEST_TIMEOUT=240.
# Switches (env): WORKERS=3, TEST_TIMEOUT=120, RUN_LIMIT_MIN=40, PG_STATEMENT_TIMEOUT=100s, TIMEOUT_METHOD=signal, LOG_DIR=$TMPDIR. One run at a time (a lock). The full pytest log and a summary
# (counts + FAILED / ERROR / timed-out list) go to LOG_DIR and the summary is printed. Exit: 0 green, 1 red, 124 run limit, 3 postgres not ready. The first run builds the two images; pytest-timeout is in the image only.
set -euo pipefail
cd "$(dirname "$0")/.."

SHIFT="${FAKE_SHIFT:?set FAKE_SHIFT, e.g. FAKE_SHIFT=+30d (+1d, +7d, +60d, +400d, +18h) or an absolute UTC moment like '2027-01-02 10:00'}"
WORKERS="${WORKERS:-3}"
TEST_TIMEOUT="${TEST_TIMEOUT:-120}"
TIMEOUT_METHOD="${TIMEOUT_METHOD:-signal}"
PG_STATEMENT_TIMEOUT="${PG_STATEMENT_TIMEOUT:-100s}"
LIMIT_MIN="${RUN_LIMIT_MIN:-40}"
command -v docker >/dev/null || { echo "docker is needed" >&2; exit 2; }

# the shift in whole seconds (signed): +30d -> 2592000; an absolute moment (UTC) -> its distance from now
case "$SHIFT" in
  [+-][0-9]*)
    N="${SHIFT#?}"; UNIT="${N##*[0-9]}"; N="${N%"$UNIT"}"
    case "${UNIT:-s}" in s) MULT=1 ;; m) MULT=60 ;; h) MULT=3600 ;; d) MULT=86400 ;; y) MULT=31536000 ;; *) echo "unknown unit in FAKE_SHIFT '$SHIFT'" >&2; exit 2 ;; esac
    case "$N" in ''|*[!0-9]*) echo "FAKE_SHIFT '$SHIFT': a whole number is needed (+30d, +18h, -2d)" >&2; exit 2 ;; esac
    OFFSET=$((N * MULT)); if [ "${SHIFT:0:1}" = "-" ]; then OFFSET=$((-OFFSET)); fi ;;
  [0-9][0-9][0-9][0-9]-*)
    TARGET="$(TZ=UTC date -j -f '%Y-%m-%d %H:%M' "$SHIFT" +%s 2>/dev/null || date -u -d "$SHIFT" +%s)" || { echo "cannot read the moment '$SHIFT' (use 'YYYY-MM-DD HH:MM', UTC)" >&2; exit 2; }
    OFFSET=$((TARGET - $(date +%s))) ;;
  *) echo "FAKE_SHIFT must be +Nd / -Nd / +Nh or 'YYYY-MM-DD HH:MM' (UTC), got '$SHIFT'" >&2; exit 2 ;;
esac

ROOT="$(pwd -P)"
GIT_COMMON="$(cd "$(git rev-parse --git-common-dir)" && pwd -P)"          # the shared .git of a worktree: git (the line-ending guard of the tests) must work inside the container
HASH="$( (cat requirements.txt constraints.txt; echo "ft-image-v4 pytest-timeout==2.4.0") | shasum | cut -c1-10)"
TESTS_IMAGE="algo-ft-tests:$HASH"
PG_IMAGE="algo-ft-pg:2"
NET="algo-ft-net-$$"
PG="algo-ft-pg-$$"
RUNNER="algo-ft-run-$$"
LOCK="${TMPDIR:-/tmp}/algo-test-shifted.lock"
LOG_DIR="${LOG_DIR:-${TMPDIR:-/tmp}}"
mkdir -p "$LOG_DIR"
LABEL="$(printf '%s' "$SHIFT" | tr -c 'A-Za-z0-9+-' '_')"
LOG="$LOG_DIR/test_shifted_${LABEL}_$(date +%Y%m%d-%H%M%S).log"
SUMMARY="${LOG%.log}.summary.txt"

cleanup() {
  trap - EXIT INT TERM HUP
  docker rm -f "$RUNNER" "$PG" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rmdir "$LOCK" >/dev/null 2>&1 || true
}
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "another shifted run is in progress (lock $LOCK) - one at a time; if it is dead: rmdir $LOCK" >&2
  exit 2
fi
trap cleanup EXIT
trap 'echo "!!! interrupted - removing the containers" >&2; exit 130' INT TERM HUP

if ! docker image inspect "$PG_IMAGE" >/dev/null 2>&1; then
  echo "building $PG_IMAGE ..."
  CTX="$(mktemp -d)"
  cat > "$CTX/shift_clock.c" <<'CSRC'
/* adds CLOCK_SHIFT_SECONDS to the wall clock (gettimeofday, clock_gettime(CLOCK_REALTIME), time) and touches nothing else, so the timers of postgres keep working */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdlib.h>
#include <sys/time.h>
#include <time.h>
static long off = 0; static int ready = 0;
static void init(void) { const char *s = getenv("CLOCK_SHIFT_SECONDS"); off = s ? atol(s) : 0; ready = 1; }
int clock_gettime(clockid_t c, struct timespec *t) {
  static int (*real)(clockid_t, struct timespec *);
  if (!real) real = dlsym(RTLD_NEXT, "clock_gettime");
  int r = real(c, t);
  if (!ready) init();
  if (r == 0 && (c == CLOCK_REALTIME || c == CLOCK_REALTIME_COARSE)) t->tv_sec += off;
  return r;
}
int gettimeofday(struct timeval *tv, void *tz) {
  static int (*real)(struct timeval *, void *);
  if (!real) real = dlsym(RTLD_NEXT, "gettimeofday");
  int r = real(tv, tz);
  if (!ready) init();
  if (r == 0 && tv) tv->tv_sec += off;
  return r;
}
time_t time(time_t *out) {
  struct timespec ts; clock_gettime(CLOCK_REALTIME, &ts);
  if (out) *out = ts.tv_sec;
  return ts.tv_sec;
}
CSRC
  printf 'FROM python:3.11 AS shim\nCOPY shift_clock.c /s/\nRUN gcc -O2 -shared -fPIC -o /s/shift_clock.so /s/shift_clock.c -ldl\nFROM postgres:15\nCOPY --from=shim /s/shift_clock.so /usr/local/lib/shift_clock.so\n' > "$CTX/Dockerfile"
  docker build -q -t "$PG_IMAGE" "$CTX" >/dev/null
  rm -rf "$CTX"
fi
if ! docker image inspect "$TESTS_IMAGE" >/dev/null 2>&1; then
  echo "building $TESTS_IMAGE ..."
  CTX="$(mktemp -d)"
  cp requirements.txt constraints.txt "$CTX/"
  printf 'FROM python:3.11\nRUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*\nCOPY requirements.txt constraints.txt /tmp/\nRUN pip install --no-cache-dir -r /tmp/requirements.txt -c /tmp/constraints.txt pytest==8.4.2 pytest-xdist==3.8.0 pytest-timeout==2.4.0\nCOPY --from=%s /usr/local/lib/shift_clock.so /usr/local/lib/shift_clock.so\n' "$PG_IMAGE" > "$CTX/Dockerfile"
  docker build -q -t "$TESTS_IMAGE" "$CTX" >/dev/null
  rm -rf "$CTX"
fi

FAKE=(-e LD_PRELOAD=/usr/local/lib/shift_clock.so -e "CLOCK_SHIFT_SECONDS=$OFFSET")                # both containers: the shim

docker network create "$NET" >/dev/null
# the postgres runs as the user ``postgres`` (not through gosu, which needs root); the server and every backend see the shifted clock
docker run -d --name "$PG" --network "$NET" --user postgres "${FAKE[@]}" -e POSTGRES_USER=algo -e POSTGRES_PASSWORD=ci-tests-only -e POSTGRES_DB=a2_auth \
  "$PG_IMAGE" -c fsync=off -c synchronous_commit=off -c full_page_writes=off -c max_connections=200 -c "statement_timeout=$PG_STATEMENT_TIMEOUT" >/dev/null
# ready = answers on TCP (the temporary server of the image's init step listens on the socket only and then stops); two answers in a row, not one
READY=0
for _ in $(seq 1 240); do
  if docker exec "$PG" pg_isready -h 127.0.0.1 -U algo -d a2_auth >/dev/null 2>&1 && docker exec "$PG" psql -h 127.0.0.1 -U algo -d a2_auth -Atqc 'select 1' >/dev/null 2>&1; then READY=1; break; fi
  sleep 0.5
done
if [ "$READY" != 1 ]; then
  echo "!!! postgres did not become ready in 2 minutes (shift $SHIFT):" >&2
  docker logs --tail 25 "$PG" 2>&1 | cut -c1-200 >&2
  exit 3
fi
docker exec "$PG" psql -U algo -d postgres -qc "CREATE DATABASE a5_auth_migrations" >/dev/null
PYCLOCK="$(docker run --rm "${FAKE[@]}" "$TESTS_IMAGE" python -c 'import datetime; print(datetime.datetime.utcnow().strftime("%F %T"))')"
echo "clock shift: $SHIFT (${OFFSET}s)  postgres now(): $(docker exec "$PG" psql -U algo -d a2_auth -Atc "select to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')")  python utcnow(): $PYCLOCK  machine: $(date -u '+%F %T')"
echo "workers: $WORKERS  per-test timeout: ${TEST_TIMEOUT}s ($TIMEOUT_METHOD)  statement_timeout: $PG_STATEMENT_TIMEOUT  run limit: ${LIMIT_MIN} min  log: $LOG"

BASE="postgresql+psycopg2://algo:ci-tests-only@$PG:5432"
STARTED="$(date +%s)"
docker run --name "$RUNNER" --network "$NET" "${FAKE[@]}" \
  -v "$ROOT:$ROOT" -v "$GIT_COMMON:$GIT_COMMON:ro" -w "$ROOT" \
  -e A2_AUTH_TEST_DATABASE_URL="$BASE/a2_auth" -e A5_AUTH_MIGRATION_TEST_DATABASE_URL="$BASE/a5_auth_migrations" -e DATABASE_URL="$BASE/a2_auth" \
  -e APP_ENV=testing -e SECRET_KEY=ci-tests-only-secret-at-least-32-characters -e REDIS_URL=redis://localhost:6379/0 -e INTERNAL_SERVICE_TOKEN=ci-tests-only-internal-token -e GEMINI_API_KEY= \
  -e PYTHONDONTWRITEBYTECODE=1 -e COLUMNS=250 -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0='*' \
  "$TESTS_IMAGE" python -m pytest -n "$WORKERS" --dist loadfile -v -rfE --tb=short -p no:cacheprovider --timeout="$TEST_TIMEOUT" --timeout-method="$TIMEOUT_METHOD" "$@" \
  > "$LOG" 2>&1 &
RUN_PID=$!

# the run limit: no ``timeout`` binary on a Mac, so the script itself watches the clock (the real one: the shifted clock lives only in the containers)
LIMITED=0
while kill -0 "$RUN_PID" 2>/dev/null; do
  if [ $(( $(date +%s) - STARTED )) -ge $((LIMIT_MIN * 60)) ]; then
    LIMITED=1
    echo "!!! RUN LIMIT: the run took more than $LIMIT_MIN minutes - the container is killed (RUN_LIMIT_MIN changes it)" | tee -a "$LOG"
    docker kill "$RUNNER" >/dev/null 2>&1 || true
    break
  fi
  sleep 5
done
set +e
wait "$RUN_PID"
RC=$?
set -e
if [ "$LIMITED" = 1 ]; then RC=124; fi

{
  echo "shift: $SHIFT (${OFFSET}s)   workers: $WORKERS   per-test timeout: ${TEST_TIMEOUT}s   duration: $(( $(date +%s) - STARTED ))s   exit code: $RC"
  if [ "$LIMITED" = 1 ]; then echo "RUN LIMIT (${LIMIT_MIN} min) HIT: the run was killed, the counts below are partial (last progress: $(grep -oE '\[ *[0-9]+%\]' "$LOG" | tail -1 | grep . || echo none yet))"; fi
  echo "result: $(grep -E '^=+ .*(passed|failed|error|no tests ran).* in [0-9.]+s' "$LOG" | tail -1 | tr -d '=' | sed 's/^ *//')"
  echo "--- timed out (pytest-timeout ${TEST_TIMEOUT}s / statement_timeout ${PG_STATEMENT_TIMEOUT}) or crashed worker:"
  { grep -E "worker 'gw[0-9]+' crashed while running" "$LOG" || true; grep -E '^(FAILED|ERROR) .*(Timeout \(>|QueryCanceled|statement timeout)' "$LOG" || true; } | sort -u | cut -c1-230
  echo "--- FAILED / ERROR (each test once):"
  # the live ``[gwN] [ 42%] FAILED path::test`` lines survive a killed run (the final «short test summary» does not)
  { grep -E '^\[gw[0-9]+\] \[ *[0-9]+%\] (FAILED|ERROR) ' "$LOG" || true; } | sed -E 's/^\[gw[0-9]+\] \[ *[0-9]+%\] //; s/ *$//' | sort -u | cut -c1-230
} > "$SUMMARY"
cat "$SUMMARY"
echo "full log: $LOG"
if [ "$RC" -ne 0 ]; then
  echo "--- the tail of the postgres log:"
  docker logs --tail 15 "$PG" 2>&1 | cut -c1-200
fi
exit "$RC"
