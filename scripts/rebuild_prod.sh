#!/usr/bin/env bash
# WP-2.12 (OPS-11, decision G-3 option A) — the production rebuild, as one script.
#
# Prod runs code older than `57bacb9` and docker-compose.yml BAKES `src/` into the api,
# worker and ui images (`COPY src/ /app/src/` in each Dockerfile), so a `restart` or a
# plain `up` changes nothing: the P1/P2 work only reaches the server through a rebuild.
# This is that rebuild, with every step's expected output printed next to it. README
# "Production rebuild (runbook)" carries the same sequence plus the failure signatures.
#
# RUN IT ON THE SERVER, from the repo root, as the user that owns the docker daemon.
# It cannot be rehearsed against the live volumes: rehearse with --dry-run, which
# changes nothing and prints every command it would run.
#
#   scripts/rebuild_prod.sh --dry-run                  # rehearse; changes nothing
#   scripts/rebuild_prod.sh --yes --rotated            # keep the data (README option B)
#   scripts/rebuild_prod.sh --yes --reset-volumes      # DEV ONLY — destroys the data
#   scripts/rebuild_prod.sh --yes --rotated --verify-migrations   # STAGING ONLY (destructive)
#
# The two mutually exclusive upgrade paths exist because of OPS-15: postgres, rabbitmq
# and minio read their *_USER / *_PASSWORD variables only when they initialise an EMPTY
# data directory. The credentials that used to be literals in docker-compose.yml
# (`finalze/finalze`, `guest/guest`, `minioadmin`) still live inside the existing
# volumes, so the new `.env` values do not reach them and the stack fails to
# authenticate. Either the volumes go (--reset-volumes) or the accounts were rotated by
# hand first (--rotated, README "Upgrading an existing deployment" option B). Refusing
# to guess is the point: a wrong guess here is either data loss or a stack that will not
# start.
#
# Exit codes
#   0  every step done
#   1  a step failed
#   2  bad usage (including: a real run without --yes)
#   3  refused — existing volumes and no upgrade path chosen (OPS-15)
#
# Hard rule: this script NEVER applies a schema change of its own. The OPS-14 constraint
# gap and the NULL-org data check print a notice with the SQL; a human runs it.

set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

readonly ENV_FILE="${ENV_FILE:-.env}"
# docker-compose.yml pins `name: finalze`, so the volumes are `finalze_<volume>`.
readonly PROJECT="${COMPOSE_PROJECT_NAME:-finalze}"
readonly API_READY_TIMEOUT="${API_READY_TIMEOUT:-300}"
readonly REQUIRED_FREE_GB="${REQUIRED_FREE_GB:-10}"
# The constraint varcand_unique_0001 adds and `_upsert_variation_candidate` depends on.
readonly VARCAND_UQ="uq_variation_candidate_org_raw_matched"

DRY_RUN=0
ASSUME_YES=0
RESET_VOLUMES=0
ROTATED=0
VERIFY_MIGRATIONS=0
RUN_SMOKE=1
SMOKE_INSECURE=1
EXPECTED_REF="feat/hitl-ui-flags"

preflight_issues=0
notices=0

# Every step, in order. Printed as the plan under --dry-run and used as the progress
# header of each step, so "the plan" and "what ran" cannot drift apart.
STEP_LABELS=(
  "preflight: repository ref and a clean working tree"
  "preflight: .env present and 'docker compose config' renders"
  "preflight: TRAEFIK_CERT_FILE / TRAEFIK_KEY_FILE resolve to files"
  "preflight: docker daemon reachable and enough free disk"
  "upgrade path: existing volumes and the OPS-15 credential choice"
  "build: docker compose build --pull"
  "start: docker compose up -d"
  "wait: the api container reports healthy"
  "schema: alembic current == head, plus the OPS-14 and NULL-org data checks"
  "verify: scripts/verify_migrations.sh --yes (only with --verify-migrations)"
  "smoke: scripts/smoke_compose_ui.py (lockdown + end-to-end + review write-back)"
  "post: docker compose ps, where the logs are, image cleanup"
)
readonly STEP_COUNT=${#STEP_LABELS[@]}

# --------------------------------------------------------------------------- output
# Colour only on a TTY: the runbook output is meant to be pasted into a ticket.
if [[ -t 1 ]]; then
  C_BOLD=$'\033[1m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_OK=$'\033[32m'; C_OFF=$'\033[0m'
else
  C_BOLD=""; C_WARN=""; C_ERR=""; C_OK=""; C_OFF=""
fi

step()   { printf '\n%s== step %s/%s : %s%s\n' "$C_BOLD" "$1" "$STEP_COUNT" "$2" "$C_OFF"; }
info()   { printf '  %s\n' "$*"; }
expect() { printf '  expect: %s\n' "$*"; }
ok()     { printf '  %sOK%s    %s\n' "$C_OK" "$C_OFF" "$*"; }
warn()   { printf '  %sWARN%s  %s\n' "$C_WARN" "$C_OFF" "$*"; }

notice() {
  notices=$((notices + 1))
  printf '\n  %s!! NOTICE !!%s %s\n' "$C_WARN" "$C_OFF" "$*"
}

die() {
  printf '\n%sABORT%s  %s\n' "$C_ERR" "$C_OFF" "$1" >&2
  printf '        see README "Production rebuild (runbook)" -> failure signatures\n' >&2
  exit "${2:-1}"
}

# A preflight failure aborts a real run. Under --dry-run it is reported and the plan
# continues, so the sequence can be rehearsed off the server (no .env, no cert, no
# daemon). What --dry-run does NOT downgrade: the --yes requirement and the OPS-15
# upgrade-path refusal. Those are about the operator's decision, not about this host.
preflight_fail() {
  if (( DRY_RUN )); then
    preflight_issues=$((preflight_issues + 1))
    printf '  %sWOULD ABORT%s  %s\n' "$C_ERR" "$C_OFF" "$*"
  else
    die "$*"
  fi
}

# --------------------------------------------------------------------------- runners
# run: a command that CHANGES something. Printed and skipped under --dry-run.
run() {
  if (( DRY_RUN )); then
    printf '  DRY-RUN  $ %s\n' "$*"
    return 0
  fi
  printf '  $ %s\n' "$*"
  "$@"
}

# capture: a READ-ONLY query. It runs in both modes — a dry run that could not look at
# the host would not be able to tell the operator which upgrade path they need.
capture() { "$@" 2>/dev/null | tr -d '\r' || true; }

compose() { docker compose "$@"; }

trim() { sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'; }

# Read ONE value out of .env WITHOUT sourcing it — sourcing would execute whatever the
# file contains. Strips a UTF-8 BOM (a Windows editor writes one and it sticks to the
# FIRST key; src/api/settings.py reads the file as utf-8-sig for exactly that reason and
# compose strips it too, so a BOM is tolerated everywhere and is never the bug you are
# looking for), CRLF, and one layer of surrounding quotes.
env_get() {
  local key="$1" line bom
  [[ -f "$ENV_FILE" ]] || return 0
  bom="$(printf '\xEF\xBB\xBF')"
  line="$(sed -e "1s/^${bom}//" -e 's/\r$//' "$ENV_FILE" \
          | grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" | tail -n1 || true)"
  [[ -n "$line" ]] || return 0
  line="${line#*=}"
  line="${line%\"}"; line="${line#\"}"
  line="${line%\'}"; line="${line#\'}"
  printf '%s' "$line"
}

usage() {
  cat <<'EOF'
scripts/rebuild_prod.sh — WP-2.12 production rebuild runbook (OPS-11, decision G-3 A).

Run it ON THE SERVER, from the repo root. compose BAKES src/ into the images, so a
restart changes nothing: the P1/P2 work reaches the server only through this rebuild.

  scripts/rebuild_prod.sh --dry-run                  # rehearse; changes nothing
  scripts/rebuild_prod.sh --yes --rotated            # keep the data (README option B)
  scripts/rebuild_prod.sh --yes --reset-volumes      # DEV ONLY — destroys the data
  scripts/rebuild_prod.sh --yes --rotated --verify-migrations   # STAGING ONLY

Options
  --dry-run             print the plan and every command; change nothing
  --yes                 required for any real run (this restarts the stack)
  --reset-volumes       DEV ONLY: `docker compose down -v` first (destroys the data)
  --rotated             the accounts inside the existing volumes were rotated by hand
                        (README "Upgrading an existing deployment", option B)
  --verify-migrations   also run scripts/verify_migrations.sh --yes (DESTRUCTIVE:
                        its step A runs `down -v`; staging, never the live volume)
  --no-smoke            skip scripts/smoke_compose_ui.py
  --trusted-cert        run the smoke WITHOUT --insecure (the host trusts the cert)
  --ref <git-ref>       expect this branch instead of feat/hitl-ui-flags
  -h, --help            this text
EOF
}

# --------------------------------------------------------------------------- args
while (( $# )); do
  case "$1" in
    --dry-run)           DRY_RUN=1 ;;
    --yes|-y)            ASSUME_YES=1 ;;
    --reset-volumes)     RESET_VOLUMES=1 ;;
    --rotated)           ROTATED=1 ;;
    --verify-migrations) VERIFY_MIGRATIONS=1 ;;
    --no-smoke)          RUN_SMOKE=0 ;;
    --trusted-cert)      SMOKE_INSECURE=0 ;;
    --ref)               shift; EXPECTED_REF="${1:-}"; [[ -n "$EXPECTED_REF" ]] || die "--ref needs a value" 2 ;;
    -h|--help)           usage; exit 0 ;;
    *)                   usage >&2; die "unknown argument: $1" 2 ;;
  esac
  shift
done

if (( RESET_VOLUMES && ROTATED )); then
  die "--reset-volumes and --rotated are mutually exclusive: one destroys the data, the other keeps it" 2
fi

# The --yes gate, checked before anything runs. This is the "nothing destructive by
# accident" rail: without --yes the only thing this script can do is print.
if (( ! DRY_RUN && ! ASSUME_YES )); then
  die "refusing to change anything without --yes. Rehearse first: scripts/rebuild_prod.sh --dry-run" 2
fi

[[ -f docker-compose.yml ]] || die "docker-compose.yml not found in ${REPO_ROOT} — run this from the repo" 2

trap 'printf "\n%sFAILED%s at line %s. The stack may be half-started: docker compose ps; docker compose logs --tail 100 api\n" "$C_ERR" "$C_OFF" "$LINENO" >&2' ERR

printf '%sFinAlze production rebuild (WP-2.12 / OPS-11)%s\n' "$C_BOLD" "$C_OFF"
printf '  repo:    %s\n' "$REPO_ROOT"
printf '  project: %s\n' "$PROJECT"
if (( DRY_RUN )); then
  printf '  mode:    %sDRY RUN — nothing is built, started, stopped or deleted%s\n' "$C_WARN" "$C_OFF"
else
  printf '  mode:    LIVE (--yes given)\n'
fi
printf '\n%sPlan%s\n' "$C_BOLD" "$C_OFF"
for i in "${!STEP_LABELS[@]}"; do
  printf '  %2s/%s  %s\n' "$((i + 1))" "$STEP_COUNT" "${STEP_LABELS[$i]}"
done

# --------------------------------------------------------------------------- 1
step 1 "${STEP_LABELS[0]}"
expect "HEAD on ${EXPECTED_REF}, 'git status --porcelain' empty"
current_ref="$(capture git rev-parse --abbrev-ref HEAD)"
current_sha="$(capture git rev-parse --short HEAD)"
info "HEAD: ${current_sha:-<unknown>} on ${current_ref:-<detached>}"
if [[ "$current_ref" != "$EXPECTED_REF" ]]; then
  preflight_fail "checked out '${current_ref:-<detached>}', expected '${EXPECTED_REF}'. The images are built FROM THE WORKING TREE (compose bakes src/), so the wrong ref rebuilds the wrong code. Pass --ref to override deliberately."
else
  ok "on ${EXPECTED_REF}"
fi
dirty="$(capture git status --porcelain)"
if [[ -n "$dirty" ]]; then
  preflight_fail "the working tree is not clean — the images would bake uncommitted changes:
$dirty"
else
  ok "working tree clean"
fi

# --------------------------------------------------------------------------- 2
step 2 "${STEP_LABELS[1]}"
expect "'docker compose config -q' prints nothing and exits 0"
if [[ ! -f "$ENV_FILE" ]]; then
  preflight_fail "${ENV_FILE} is missing. cp .env.example .env and fill in EVERY variable in its REQUIRED block (each comment gives the generation command)."
else
  ok "${ENV_FILE} present"
fi
# `docker compose config` is the authority, not a hand-rolled variable list: every
# credential is a ${VAR:?} in docker-compose.yml and `:?` fires on EMPTY as well as
# unset, so a verbatim copy of .env.example cannot render. The error names the first
# offender. This is read-only — it renders the model, it does not touch the project.
if config_err="$(compose config -q 2>&1)"; then
  ok "compose renders — every \${VAR:?} is set and non-empty"
else
  preflight_fail "docker compose config failed:
${config_err}
An 'invalid interpolation format' / 'required variable is missing' line names the variable: it is unset or EMPTY in ${ENV_FILE}."
fi

# --------------------------------------------------------------------------- 3
step 3 "${STEP_LABELS[2]}"
expect "both paths exist and are FILES (a directory is the classic TLS failure)"
check_cert_var() {
  local name="$1" path="$2"
  if [[ -z "$path" ]]; then
    preflight_fail "${name} is empty in ${ENV_FILE}. Generate a pair with scripts/gen_selfsigned_cert.sh <host>, or point it at the bank PEM."
    return 0
  fi
  if [[ -d "$path" ]]; then
    preflight_fail "${name}=${path} is a DIRECTORY. Docker Desktop creates one where a bind source is missing (create_host_path:false is ignored there); traefik then aborts every handshake with TLSV1_ALERT_INTERNAL_ERROR. Delete it and point the variable at the real file."
    return 0
  fi
  if [[ ! -f "$path" ]]; then
    preflight_fail "${name}=${path} does not exist."
    return 0
  fi
  ok "${name} -> ${path}"
  return 0
}
cert_file="$(env_get TRAEFIK_CERT_FILE)"
key_file="$(env_get TRAEFIK_KEY_FILE)"
check_cert_var TRAEFIK_CERT_FILE "$cert_file"
check_cert_var TRAEFIK_KEY_FILE "$key_file"
if [[ -f "$key_file" ]] && command -v stat >/dev/null 2>&1; then
  key_mode="$(stat -c '%a' "$key_file" 2>/dev/null || echo "")"
  if [[ -n "$key_mode" && "$key_mode" != "600" && "$key_mode" != "400" ]]; then
    warn "the private key is mode ${key_mode}; it should be 600 and its directory 700 (gen_selfsigned_cert.sh sets the file mode but cannot fix the parent)"
  fi
fi
info "a bank certificate delivered as leaf + separate intermediate must be concatenated"
info "LEAF FIRST into one PEM — traefik serves an incomplete chain with no error at all."

# --------------------------------------------------------------------------- 4
step 4 "${STEP_LABELS[3]}"
expect "a docker server version, and >= ${REQUIRED_FREE_GB} GB free on ${REPO_ROOT}"
if ! command -v docker >/dev/null 2>&1; then
  preflight_fail "docker not found on PATH"
else
  server_version="$(capture docker info --format '{{.ServerVersion}}' | trim)"
  if [[ -z "$server_version" ]]; then
    preflight_fail "the docker daemon is not reachable (is Docker Desktop / dockerd running, and is this user in the docker group?)"
  else
    ok "docker daemon ${server_version}"
  fi
fi
if command -v df >/dev/null 2>&1; then
  free_kb="$(df -Pk . 2>/dev/null | awk 'NR==2 {print $4}' || echo "")"
  if [[ -n "$free_kb" ]]; then
    free_gb=$(( free_kb / 1024 / 1024 ))
    if (( free_gb < REQUIRED_FREE_GB )); then
      preflight_fail "only ${free_gb} GB free; the worker image alone is multi-GB and a failed build leaves layers behind. Free space or run 'docker image prune' first."
    else
      ok "${free_gb} GB free"
    fi
  else
    warn "could not read free disk space"
  fi
fi

# --------------------------------------------------------------------------- 5
step 5 "${STEP_LABELS[4]}"
expect "either no data volumes yet, or exactly one of --reset-volumes / --rotated"
all_volumes="$(capture docker volume ls -q)"
existing=()
for vol in pgdata rabbitmqdata miniodata; do
  if printf '%s\n' "$all_volumes" | grep -qx "${PROJECT}_${vol}"; then
    existing+=("${PROJECT}_${vol}")
  fi
done

if (( ${#existing[@]} == 0 )); then
  ok "no ${PROJECT}_{pgdata,rabbitmqdata,miniodata} volume exists — first install, the .env credentials initialise them"
  if (( RESET_VOLUMES )); then
    warn "--reset-volumes given but there is nothing to reset"
  fi
else
  info "existing data volumes: ${existing[*]}"
  info "OPS-15: postgres/rabbitmq/minio read *_USER / *_PASSWORD only when they initialise an"
  info "EMPTY data directory, so the accounts INSIDE these volumes are still the old ones."
  if (( RESET_VOLUMES )); then
    printf '\n  %s*** DATA LOSS ***%s `docker compose down -v` deletes %s.\n' "$C_ERR" "$C_OFF" "${existing[*]}"
    printf '  Every uploaded document, extraction result and object-storage bucket goes with them.\n'
    printf '  This branch is for a development or staging host. Never the bank server.\n\n'
    run docker compose down -v
  elif (( ROTATED )); then
    ok "--rotated: you confirm README \"Upgrading an existing deployment\" option B was applied"
    info "i.e. the postgres role was renamed + repassworded, the rabbitmq user was added with"
    info "permissions and tags and 'guest' deleted, and MINIO_ROOT_* in .env still matches what"
    info "the miniodata volume actually holds (or a new account was made with 'mc admin user add')."
    info "If that is not true, step 8 fails with an authentication error from postgres or rabbitmq."
  else
    die "existing data volumes (${existing[*]}) and no upgrade path chosen (OPS-15).
The credentials in those volumes are NOT the ones in ${ENV_FILE}, so \`up\` would fail to authenticate.
Choose, and say so on the command line:
  --rotated         you already rotated the accounts by hand  (README \"Upgrading an existing deployment\", option B)
  --reset-volumes   destroy the data and re-initialise        (development only)" 3
  fi
fi

# --------------------------------------------------------------------------- 6
step 6 "${STEP_LABELS[5]}"
expect "every service image rebuilt; 'COPY src/ /app/src/' is why a restart is not enough"
info "the gpu profile is deliberately not built — 'docker compose --profile gpu up -d' is a separate, later step"
run docker compose build --pull

# --------------------------------------------------------------------------- 7
step 7 "${STEP_LABELS[6]}"
expect "postgres, redis, rabbitmq, minio, api, worker, flower, ui, traefik created/recreated"
run docker compose up -d

# --------------------------------------------------------------------------- 8
step 8 "${STEP_LABELS[7]}"
expect "the api container's health goes healthy within ${API_READY_TIMEOUT}s"
info "api starts as: python -m api.bootstrap && python -m api.migrate && uvicorn ... — the '&&'"
info "is load-bearing, so a failed migration exits the container instead of serving."
api_health() {
  local cid
  cid="$(capture compose ps -q api | head -n1)"
  [[ -n "$cid" ]] || { echo "no-container"; return 0; }
  capture docker inspect --format '{{.State.Health.Status}}' "$cid" | head -n1
}
if (( DRY_RUN )); then
  printf '  DRY-RUN  $ docker inspect --format {{.State.Health.Status}} <api> every 3s until "healthy" (max %ss)\n' "$API_READY_TIMEOUT"
else
  waited=0
  until [[ "$(api_health)" == "healthy" ]]; do
    if (( waited >= API_READY_TIMEOUT )); then
      compose logs --tail 60 api || true
      die "the api container is still '$(api_health)' after ${API_READY_TIMEOUT}s. The log above is the real error: a missing FINALZE_SECRET_KEY, a credential the volume does not know (OPS-15), or an alembic failure."
    fi
    sleep 3
    waited=$((waited + 3))
  done
  ok "api healthy after ${waited}s"
fi

# --------------------------------------------------------------------------- 9
step 9 "${STEP_LABELS[8]}"
expect "'alembic current' ends with '<revision> (head)' and names the chain's single head"
info "api.migrate decides any stamp BY INSPECTING the schema (alembic/versions/README.md):"
info "  (a) alembic_version present                -> upgrade head"
info "  (b) absent, corrections.statement_id absent -> stamp improvement_loop_0002, upgrade head"
info "  (c) absent, corrections.statement_id present -> stamp correction_stmt_key_0003, upgrade head"
info "Case (c) is today's prod and the stamp is deliberately one revision behind the head:"
info "the following 'upgrade head' is what applies audit_target_index_0004."
pg_user="$(env_get POSTGRES_USER)"
pg_db="$(env_get POSTGRES_DB)"
if (( DRY_RUN )); then
  printf '  DRY-RUN  $ docker compose exec -T api python -m alembic heads\n'
  printf '  DRY-RUN  $ docker compose exec -T api python -m alembic current\n'
  printf '  DRY-RUN  $ docker compose exec -T postgres psql -U %s -d %s -tAc %s\n' \
    "${pg_user:-<POSTGRES_USER>}" "${pg_db:-<POSTGRES_DB>}" \
    "\"SELECT 1 FROM pg_constraint WHERE conname='${VARCAND_UQ}'\"     # OPS-14"
  printf '  DRY-RUN  $ docker compose exec -T postgres psql -U %s -d %s -tAc %s\n' \
    "${pg_user:-<POSTGRES_USER>}" "${pg_db:-<POSTGRES_DB>}" \
    "\"SELECT count(*) FROM issues WHERE org_id IS NULL\"     # + remediation_actions"
else
  heads_raw="$(capture compose exec -T api python -m alembic heads)"
  head_count="$(printf '%s\n' "$heads_raw" | grep -c '[^[:space:]]' || true)"
  [[ "$head_count" == "1" ]] || die "expected exactly one alembic head, got ${head_count}:
${heads_raw}
A branched chain means 'the head' is ambiguous — merge the revisions before rebuilding."
  head_rev="$(printf '%s\n' "$heads_raw" | grep '[^[:space:]]' | head -n1 | awk '{print $1}')"
  current="$(capture compose exec -T api python -m alembic current | tr '\n' ' ' | trim)"
  info "alembic heads:   ${head_rev}"
  info "alembic current: ${current:-<empty>}"
  if [[ -z "$current" ]]; then
    compose logs --tail 60 api || true
    die "'alembic current' printed nothing — the migrate step never completed. The api log above names the failure."
  fi
  [[ "$current" == *"${head_rev} (head)"* ]] || die "the database is at '${current}', not '${head_rev} (head)'."
  ok "database at ${head_rev} (head)"

  # OPS-14 — the known gap in api.migrate case (b). Stamping improvement_loop_0002 marks
  # varcand_unique_0001 as applied without running it, so a volume whose
  # variation_candidates table predates 0001 never gets the UNIQUE constraint that
  # `_upsert_variation_candidate` (src/api/routers/review.py) relies on. Reported, never
  # auto-applied: the fix deletes rows, and nothing here deletes rows unattended.
  if [[ -z "$pg_user" || -z "$pg_db" ]]; then
    warn "POSTGRES_USER / POSTGRES_DB not readable from ${ENV_FILE}; skipping the OPS-14 and NULL-org checks. Run them by hand (README)."
  else
    psql_q() {
      capture compose exec -T postgres psql -U "$pg_user" -d "$pg_db" -v ON_ERROR_STOP=1 -tAc "$1" | trim
    }
    have_uq="$(psql_q "SELECT 1 FROM pg_constraint WHERE conname='${VARCAND_UQ}'")"
    if [[ "$have_uq" == "1" ]]; then
      ok "OPS-14: ${VARCAND_UQ} is present"
    else
      notice "OPS-14 — ${VARCAND_UQ} is MISSING on this volume."
      cat <<EOF
  This volume took api.migrate case (b): the stamp of improvement_loop_0002 marked
  varcand_unique_0001 applied without running it, so its UNIQUE constraint was never
  created. The HITL upsert in src/api/routers/review.py depends on it — without it the
  same variation is inserted again instead of bumping seen_count.

  NOT APPLIED AUTOMATICALLY (step 1 deletes rows). Take a dump first, then run by hand:

    docker compose exec -T postgres psql -U ${pg_user} -d ${pg_db} -v ON_ERROR_STOP=1 <<'SQL'
    WITH grp AS (
        SELECT (array_agg(id ORDER BY created_at, id))[1] AS keep_id, SUM(seen_count) AS total
        FROM variation_candidates GROUP BY org_id, raw_text, matched_to HAVING COUNT(*) > 1)
    UPDATE variation_candidates v SET seen_count = g.total FROM grp g WHERE v.id = g.keep_id;
    DELETE FROM variation_candidates v USING (
        SELECT id, ROW_NUMBER() OVER (PARTITION BY org_id, raw_text, matched_to
                                      ORDER BY created_at, id) AS rn
        FROM variation_candidates) d WHERE v.id = d.id AND d.rn > 1;
    ALTER TABLE variation_candidates
      ADD CONSTRAINT ${VARCAND_UQ} UNIQUE (org_id, raw_text, matched_to);
SQL

  (Those first two statements are varcand_unique_0001's own body: the constraint cannot
  be added while duplicates exist.)
EOF
    fi

    # The Issue / RemediationAction org filters are unconditional, so any row written
    # before org scoping existed (org_id IS NULL) becomes invisible after the rebuild.
    # Master-Plan: "data check before the WP-2.12 rebuild".
    orphan_issues="$(psql_q "SELECT count(*) FROM issues WHERE org_id IS NULL")"
    orphan_actions="$(psql_q "SELECT count(*) FROM remediation_actions WHERE org_id IS NULL")"
    if [[ "${orphan_issues:-0}" == "0" && "${orphan_actions:-0}" == "0" ]]; then
      ok "no NULL-org issues / remediation_actions rows"
    else
      notice "${orphan_issues} issues and ${orphan_actions} remediation_actions rows have org_id IS NULL."
      info "The improvement-loop endpoints filter by org unconditionally, so those rows are"
      info "invisible from now on. Re-home them to the owning org, or accept the loss:"
      info "  UPDATE issues SET org_id = '<org-uuid>' WHERE org_id IS NULL;"
      info "  UPDATE remediation_actions SET org_id = '<org-uuid>' WHERE org_id IS NULL;"
    fi
  fi
fi

# --------------------------------------------------------------------------- 10
step 10 "${STEP_LABELS[9]}"
if (( ! VERIFY_MIGRATIONS )); then
  info "skipped (pass --verify-migrations to run it)"
  info "It belongs to a STAGING run: its step A is 'docker compose down -v', which deletes"
  info "the pgdata / rabbitmqdata / miniodata volumes of this project. Never on the live host."
else
  printf '\n  %s*** DESTRUCTIVE ***%s scripts/verify_migrations.sh --yes starts with `docker compose down -v`.\n' "$C_ERR" "$C_OFF"
  printf '  Only continue if this host is staging and its data is expendable.\n\n'
  # verify_migrations.sh hardcodes `psql -U finalze -d finalze`; since WP-1.2 the pair
  # comes from .env and may be anything. Name the mismatch rather than let its psql
  # checks fail as if the migration were broken.
  if [[ -n "$pg_user" && "$pg_user" != "finalze" ]] || [[ -n "$pg_db" && "$pg_db" != "finalze" ]]; then
    warn "scripts/verify_migrations.sh connects as psql -U finalze -d finalze, but this deployment uses -U ${pg_user} -d ${pg_db}: its psql-based checks will FAIL for that reason alone. Run it on a stack whose POSTGRES_USER/DB are 'finalze', or fix the script first."
  fi
  expect "'N check(s) run, 0 failed' and 'OK: every container start ended at <head> (head)'"
  run scripts/verify_migrations.sh --yes
fi

# --------------------------------------------------------------------------- 11
step 11 "${STEP_LABELS[10]}"
if (( ! RUN_SMOKE )); then
  info "skipped (--no-smoke)"
else
  smoke_python() {
    local candidate
    if [[ -n "${SMOKE_PYTHON:-}" ]]; then printf '%s' "$SMOKE_PYTHON"; return 0; fi
    for candidate in venv/Scripts/python.exe venv/Scripts/python venv/bin/python; do
      if [[ -x "$candidate" ]]; then printf '%s' "$candidate"; return 0; fi
    done
    if command -v python3 >/dev/null 2>&1; then printf 'python3'; return 0; fi
    printf 'python'
    return 0
  }
  smoke_py="$(smoke_python)"
  info "interpreter: ${smoke_py} (override with SMOKE_PYTHON=...)"
  info "run it ON the host: it probes https://127.0.0.1:8000 and http://127.0.0.1:8501,"
  info "and 8501 is loopback-only until SEC-5 lands."
  info "It submits Samples/Bilan 1.jpg through the real worker and leaves ONE validated"
  info "document with one correction behind — that write IS the P2 exit check."
  # The smoke needs the bootstrap admin; export it without ever printing the password.
  admin_email="$(env_get FINALZE_ADMIN_EMAIL)"
  admin_password="$(env_get FINALZE_ADMIN_PASSWORD)"
  if [[ -z "$admin_email" || -z "$admin_password" ]]; then
    warn "FINALZE_ADMIN_EMAIL / FINALZE_ADMIN_PASSWORD not readable from ${ENV_FILE}; export them yourself or the smoke exits on 'set FINALZE_ADMIN_PASSWORD'."
  else
    export FINALZE_ADMIN_EMAIL="$admin_email"
    export FINALZE_ADMIN_PASSWORD="$admin_password"
    info "FINALZE_ADMIN_EMAIL exported from ${ENV_FILE} (password exported, not printed)"
  fi
  expect "'lockdown OK — N internal paths 404, 2 health paths 200' then 'ALL CHECKS PASSED'"
  if (( SMOKE_INSECURE )); then
    info "--insecure: correct for scripts/gen_selfsigned_cert.sh. Drop it (--trusted-cert) once the host trusts the certificate."
    run "$smoke_py" scripts/smoke_compose_ui.py --insecure
  else
    run "$smoke_py" scripts/smoke_compose_ui.py
  fi
fi

# --------------------------------------------------------------------------- 12
step 12 "${STEP_LABELS[11]}"
run docker compose ps
info "logs:        docker compose logs -f api worker traefik"
info "review UI:   ssh -L 8501:127.0.0.1:8501 user@host  ->  http://localhost:8501"
info "flower:      ssh -L 5555:127.0.0.1:5555 user@host  (basic auth: FLOWER_BASIC_AUTH)"
info "rabbit/minio: ssh -L 15672:127.0.0.1:15672 -L 9001:127.0.0.1:9001 user@host"
info "reclaim the superseded image layers ONLY once you are satisfied with the new stack:"
info "  docker image prune -f        # dangling layers only; not run from here"
info "G-3: the cloud firewall must still restrict inbound 8000/tcp to the owner's IP."

printf '\n%s%s%s\n' "$C_BOLD" "------------------------------------------------------------" "$C_OFF"
if (( DRY_RUN )); then
  printf '%sDRY RUN complete — nothing was built, started, stopped or deleted.%s\n' "$C_BOLD" "$C_OFF"
  if (( preflight_issues )); then
    printf '%s%s preflight issue(s) would have aborted a real run (marked WOULD ABORT above).%s\n' "$C_WARN" "$preflight_issues" "$C_OFF"
  fi
  printf 'Re-run with --yes (plus --rotated or --reset-volumes) to perform it.\n'
else
  printf '%sREBUILD COMPLETE%s\n' "$C_OK" "$C_OFF"
fi
(( notices == 0 )) || printf '%s%s notice(s) above need a human decision.%s\n' "$C_WARN" "$notices" "$C_OFF"
exit 0
