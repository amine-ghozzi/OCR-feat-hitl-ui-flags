#!/usr/bin/env bash
# WP-1.4 (OPS-1 / OPS-2) — prove the API container always ends at the head revision.
#
# `python -m api.migrate` decides whether a stamp is owed by INSPECTING the schema, so the only
# honest proof is to put a real Postgres volume into each shape and look at `alembic current`:
#
#   A. fresh volume        -> `create_all` builds the head-equivalent schema, unversioned
#   B. pre-`dc1016c` volume -> same, minus corrections.statement_id / schema_version
#   C. a correction write   -> the columns are not just present, they work
#
# Postgres only: migration `varcand_unique_0001` uses window functions and cannot run on SQLite.
# The pure decision function is covered by tests/test_migrate_decision.py.
#
# DESTRUCTIVE: step A runs `docker compose down -v`, which deletes the pgdata/minio/rabbitmq
# volumes of the `finalze` compose project. Pass --yes to confirm.
#
# Usage:  scripts/verify_migrations.sh --yes
# Exit:   0 = every check passed, 1 = a check failed, 2 = bad usage / prerequisite missing.

set -euo pipefail

# The head is READ FROM THE CHAIN at runtime, never hardcoded: every new revision would
# otherwise silently break this script (as `audit_target_index_0004` did when it was added).
HEAD_REVISION=""
EXPECTED_CURRENT=""
readonly API_READY_TIMEOUT=180
readonly AUDIT_INDEX="ix_audit_events_target"

# Fixed ids so a re-run cleans up after a previous one.
readonly ORG_ID="00000000-0000-4000-a000-00000000e14a"
readonly DOC_ID="00000000-0000-4000-a000-00000000d14d"
readonly CORR_ID="00000000-0000-4000-a000-00000000c14c"

cd "$(dirname "$0")/.."

checks_run=0
checks_failed=0

log()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
pass() { checks_run=$((checks_run + 1)); printf '  PASS  %s\n' "$*"; }
fail() {
  checks_run=$((checks_run + 1))
  checks_failed=$((checks_failed + 1))
  printf '  FAIL  %s\n' "$*"
}

# check <label> <expected-substring> <actual>
check() {
  local label="$1" expected="$2" actual="$3"
  if [[ "$actual" == *"$expected"* ]]; then
    pass "$label"
  else
    fail "$label (expected to contain '${expected}', got '${actual}')"
  fi
}

check_equal() {
  local label="$1" expected="$2" actual="$3"
  if [[ "$actual" == "$expected" ]]; then
    pass "$label"
  else
    fail "$label (expected '${expected}', got '${actual}')"
  fi
}

compose() { docker compose "$@"; }

trim() { sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'; }

# `alembic current` as the api container sees it (one line, whitespace collapsed).
alembic_current() {
  compose exec -T api python -m alembic current 2>/dev/null | tr -d '\r' | tr '\n' ' ' | trim || true
}

# The chain's head, as the container's own alembic reports it. A branched chain has more than
# one head, and "the expected revision" would then be ambiguous — so refuse rather than guess.
resolve_head() {
  local raw head_count
  raw="$(compose exec -T api python -m alembic heads 2>/dev/null | tr -d '\r')"
  head_count="$(printf '%s\n' "$raw" | grep -c '[^[:space:]]' || true)"
  if [[ "$head_count" -ne 1 ]]; then
    echo "Expected exactly one alembic head, got ${head_count}:" >&2
    printf '%s\n' "$raw" >&2
    exit 1
  fi
  # A head line looks like "<revision> (head)"; take the revision token.
  printf '%s\n' "$raw" | grep '[^[:space:]]' | head -n1 | awk '{print $1}'
}

psql_query() {
  compose exec -T postgres psql -U finalze -d finalze -v ON_ERROR_STOP=1 -tAc "$1" \
    | tr -d '\r' | trim || true
}

psql_exec() {
  compose exec -T postgres psql -U finalze -d finalze -v ON_ERROR_STOP=1 -c "$1" >/dev/null
}

# Compose's `ps --format` takes no Go template, so read the health off the container itself.
api_health() {
  local cid
  cid="$(compose ps -q api 2>/dev/null | tr -d '\r' | head -n1)"
  [[ -n "$cid" ]] || { echo "no-container"; return 0; }
  docker inspect --format '{{.State.Health.Status}}' "$cid" 2>/dev/null | tr -d '\r' || echo "unknown"
}

wait_for_api() {
  local waited=0
  until [[ "$(api_health)" == "healthy" ]]; do
    if (( waited >= API_READY_TIMEOUT )); then
      fail "api container did not become healthy within ${API_READY_TIMEOUT}s"
      compose logs --tail 40 api || true
      return 1
    fi
    sleep 3
    waited=$((waited + 3))
  done
  return 0
}

[[ "${1:-}" == "--yes" ]] || {
  echo "Refusing to run without --yes: step A deletes the compose volumes." >&2
  exit 2
}
command -v docker >/dev/null || { echo "docker not found" >&2; exit 2; }

# ---------------------------------------------------------------------------
log "A. Fresh volume -> head"
# ---------------------------------------------------------------------------
compose down -v
compose up -d --build api
wait_for_api || true

# `resolve_head` runs in a subshell, so its `exit 1` only ends that subshell; `set -e` then
# aborts on the failed assignment. The explicit empty check makes that dependency visible and
# also catches an alembic that printed nothing at all.
HEAD_REVISION="$(resolve_head)"
[[ -n "$HEAD_REVISION" ]] || { echo "could not resolve the alembic head" >&2; exit 1; }
EXPECTED_CURRENT="${HEAD_REVISION} (head)"
readonly HEAD_REVISION EXPECTED_CURRENT
printf '  head revision (from the chain): %s\n' "$HEAD_REVISION"

check "fresh volume ends at the head revision" "$EXPECTED_CURRENT" "$(alembic_current)"
check_equal "the audit-target index exists on a fresh volume" "1" \
  "$(psql_query "SELECT 1 FROM pg_indexes WHERE indexname = '${AUDIT_INDEX}'")"

# ---------------------------------------------------------------------------
log "B. Simulated pre-dc1016c volume -> head, columns restored"
# ---------------------------------------------------------------------------
# Undo what `correction_stmt_key_0003` did and remove Alembic's bookkeeping, leaving exactly the
# shape a volume built before `dc1016c` has. The audit-target index goes too: it is created by
# `audit_target_index_0004` and did not exist on such a volume, so leaving it behind would make
# the simulation flatter than the real thing and hide a failure to re-create it.
psql_exec "ALTER TABLE corrections DROP COLUMN statement_id;
           ALTER TABLE corrections DROP COLUMN schema_version;
           DROP INDEX IF EXISTS ${AUDIT_INDEX};
           DROP TABLE alembic_version;"

check_equal "the simulated volume really is unversioned" "0" \
  "$(psql_query "SELECT count(*) FROM information_schema.tables
                  WHERE table_schema='public' AND table_name='alembic_version'")"
check_equal "the simulated volume really lacks the 0003 columns" "0" \
  "$(psql_query "SELECT count(*) FROM information_schema.columns
                  WHERE table_name='corrections'
                    AND column_name IN ('statement_id','schema_version')")"
check_equal "the simulated volume really lacks the audit-target index" "0" \
  "$(psql_query "SELECT count(*) FROM pg_indexes WHERE indexname = '${AUDIT_INDEX}'")"

compose restart api
wait_for_api || true

check "pre-dc1016c volume ends at the head revision" "$EXPECTED_CURRENT" "$(alembic_current)"
check_equal "both 0003 columns are back" "2" \
  "$(psql_query "SELECT count(*) FROM information_schema.columns
                  WHERE table_name='corrections'
                    AND column_name IN ('statement_id','schema_version')")"
check_equal "the audit-target index was re-created by 0004" "1" \
  "$(psql_query "SELECT 1 FROM pg_indexes WHERE indexname = '${AUDIT_INDEX}'")"

# ---------------------------------------------------------------------------
log "C. A statement-scoped correction write succeeds"
# ---------------------------------------------------------------------------
# The application route is PATCH /api/v1/documents/{document_id}/cells (src/api/routers/review.py),
# which needs a token, an uploaded document and a completed job. What OPS-1 is actually about is
# whether the migrated columns accept a write, so this inserts the same row the route would —
# document + org first, for the foreign keys — and rolls the fixture out again.
psql_exec "INSERT INTO orgs (id, name, active)
             VALUES ('${ORG_ID}', 'wp14-verify', true)
             ON CONFLICT (id) DO NOTHING;
           INSERT INTO documents (id, org_id, filename, storage_key, accounting_standard, state)
             VALUES ('${DOC_ID}', '${ORG_ID}', 'wp14-verify.pdf', 'wp14/verify.pdf', 'NCT', 'draft')
             ON CONFLICT (id) DO NOTHING;
           INSERT INTO corrections
             (id, document_id, org_id, table_index, statement_id, schema_version,
              row_index, column_name, old_value, new_value)
             VALUES ('${CORR_ID}', '${DOC_ID}', '${ORG_ID}', 0, 1, 2, 3, 'n', '1', '2');"

check_equal "the correction row was written with statement_id + schema_version" "1|2" \
  "$(psql_query "SELECT statement_id || '|' || schema_version
                   FROM corrections WHERE id='${CORR_ID}'")"

psql_exec "DELETE FROM corrections WHERE id='${CORR_ID}';
           DELETE FROM documents   WHERE id='${DOC_ID}';
           DELETE FROM orgs        WHERE id='${ORG_ID}';"

# ---------------------------------------------------------------------------
log "Summary"
# ---------------------------------------------------------------------------
printf '  %d check(s) run, %d failed\n\n' "$checks_run" "$checks_failed"
(( checks_failed == 0 )) || exit 1
echo "OK: every container start ended at ${EXPECTED_CURRENT}."
