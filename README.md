# FinAlze OCR PoC

Banking financial statement digitization platform for balance sheets, P&L statements, and cash flow statements.

> **Project status (2026-09-14):** the consolidated remediation roadmap, issue register and open decisions live in
> [`context/Master-Plan.md`](context/Master-Plan.md), [`context/Decision-Form.md`](context/Decision-Form.md) and
> [`context/Project-Backlog.md`](context/Project-Backlog.md) — `context/` opens as an Obsidian vault.

## Refonte du parcours FinAI (27 septembre 2026)

Le [compte rendu et plan validé](context/Documentation/FinAI-Compte-rendu-et-plan-interface.html)
fait partie de la documentation de référence. Le [suivi de développement](context/FinAI-Implementation.md)
précise les fonctions livrées, les vérifications et les travaux restants.

## Quick Start

```bash
# Activate virtual environment
.\venv\Scripts\Activate  # Windows
source venv/bin/activate  # Linux/Mac

# Install dependencies
pip install -r requirements.txt

# Run sample extraction
python src/pipeline.py --input Samples/bilan1.docx --output output/
```

## Project Structure

```
OCR/
├── context/              # Research documents and plans
├── Samples/              # Test documents (17 files, 16 distinct; see context/Master-Plan.md §1)
├── src/                  # Source code
│   ├── pipeline.py       # Main orchestrator
│   ├── preprocessing/    # Language detection, doc-type classifier
│   ├── extraction/       # OCR engines (Docling, Marker, Surya)
│   ├── accounting/       # NER, schema mapping, validation
│   └── hitl/             # Streamlit validation UI
├── schemas/              # Pandera schemas for IFRS/NCT/SYSCOHADA
├── output/               # Extraction results
├── tests/                # Unit and integration tests
└── requirements.txt      # Python dependencies
```

## Running the dockerized stack (API + worker + HITL UI)

**The stack publishes exactly one port off-box: `8000/tcp`, TLS, terminated by
Traefik.** Everything else — the review UI (8501), Flower (5555), RabbitMQ
management (15672), the MinIO console (9001) — is bound to `127.0.0.1` and is
reached over an SSH tunnel. The `api` and `worker` services publish nothing at
all; the API is reachable only through Traefik.

```bash
# 1. Configuration — REQUIRED. Every credential is a ${VAR:?} in docker-compose.yml:
#    no defaults, and `docker compose config` refuses to render while any one of them
#    is unset OR EMPTY. .env.example ships every required variable empty, so a
#    verbatim copy cannot boot a stack — each comment gives the generation command.
cp .env.example .env && "$EDITOR" .env
openssl rand -hex 32              # → FINALZE_SECRET_KEY (the API will not start without it)

# 2. TLS certificate for port 8000. Self-signed, SAN = the host clients will type:
scripts/gen_selfsigned_cert.sh finalze.example.internal
#    → writes certs/server.{crt,key} (gitignored) and prints the two .env lines.
#    On the server: chmod 700 certs && chmod 600 certs/server.key (the script sets
#    the file modes, but cannot fix a world-readable parent directory).
#    A bank-provided certificate replaces this step: point the SAME two variables
#    (TRAEFIK_CERT_FILE / TRAEFIK_KEY_FILE) at its PEM files — nothing else changes.
#    Delivered as leaf + separate intermediate? Concatenate LEAF FIRST into one file
#    (`cat leaf.crt intermediate.crt > server.crt`): traefik serves an incomplete
#    chain without any error and clients lacking the intermediate reject it.

# 3. Build and start (postgres/redis/rabbitmq/minio + api + worker + ui + traefik)
docker compose up -d --build

# 4. Reach the services
#    API:  https://<host>:8000       ← the only port open on the network
#    Everything else, from your workstation, over SSH:
ssh -L 8501:127.0.0.1:8501 user@host      # HITL review UI → http://localhost:8501
ssh -L 5555:127.0.0.1:5555 user@host      # Flower (basic auth: FLOWER_BASIC_AUTH)
ssh -L 15672:127.0.0.1:15672 -L 9001:127.0.0.1:9001 user@host   # RabbitMQ · MinIO

# 5. Credentials come from .env only (FINALZE_ADMIN_EMAIL / FINALZE_ADMIN_PASSWORD).
#    The UI container talks to the API in-network (FINALZE_API_URL=http://api:8000);
#    reviewer approvals write back through /api/v1/review (corrections + state).

# 6. End-to-end smoke — first asserts the host lockdown through the public port
#    (every internal path 404, health 200, the configured certificate actually
#    served), then submits Samples/Bilan 1.jpg through the real worker and verifies
#    canonical-table rehydration, flags and review write-back. Run it ON the host;
#    --insecure is for a self-signed certificate:
venv/Scripts/python scripts/smoke_compose_ui.py --insecure
```

What is deliberately unreachable from outside (SEC-7/8/9/10, GOV-2):
- **Traefik routes by an allowlist, not a denylist.** The `api` router matches only
  `PathPrefix(/api/v1)`, `Path(/healthz)` and `Path(/readyz)`; a catch-all router
  sends *everything else* through a `replacePath` middleware that rewrites the path
  to a route FastAPI never registered, so the real handler is never reached
  (`traefik/dynamic.yml`). `/docs`, `/redoc`, `/openapi.json`, `/metrics` and
  anything added tomorrow return **404** by construction. An earlier denylist of
  those four literal prefixes was bypassable — with `FINALZE_ROOT_PATH=/finalze`,
  `/finalze/openapi.json` sailed through — which is why `FINALZE_ROOT_PATH` is not
  passed to any container and `src/api/settings.py` says it must stay empty.
- `FINALZE_DOCS_ENABLED` defaults to `false`, so `/docs` and `/redoc` do not exist
  on the app either — a second, independent lock. Set it `true` only on a dev box.
- TLS is pinned at **1.2 minimum** with forward-secret AEAD ciphers only
  (`tls.options.default`). `sniStrict` is deliberately off: tunnelled clients and
  the smoke script connect to an IP literal and send no SNI.
- Traefik runs on the **file provider**: no Docker socket is mounted, no
  dashboard, no `--api.insecure`. A container cannot publish a route by writing
  a label. Its `--ping` endpoint lives on the internal, unpublished entrypoint.
- The UI has **no per-user login yet**, which is why 8501 is loopback-only and
  goes through the tunnel above. This changes when SEC-5 lands.
- `Samples/` is **not** mounted into any container — it holds client financial
  statements. The UI's sample picker simply disappears when the directory is
  absent.

### Upgrading an existing deployment

The credentials used to be literals in `docker-compose.yml` (`finalze/finalze`,
`guest/guest`, `minioadmin`). **Those accounts still live inside the existing
`pgdata`, `rabbitmqdata` and `miniodata` volumes** — Postgres, RabbitMQ and MinIO
only read their `*_USER`/`*_PASSWORD` variables when they initialise an *empty* data
directory. Starting the new compose against old volumes therefore fails to
authenticate. Pick one before `up`:

```bash
# A — development only: discard the data and re-initialise with the new credentials.
docker compose down -v          # DESTROYS documents, results and object storage

# B — keep the data: rotate each account to the values now in .env, then `up`.
docker compose up -d postgres rabbitmq minio          # old compose file, old creds
docker compose exec postgres psql -U finalze -d finalze \
  -c "ALTER USER finalze RENAME TO \"$NEW_PG_USER\";" \
  -c "ALTER USER \"$NEW_PG_USER\" PASSWORD '$NEW_PG_PASSWORD';"
docker compose exec rabbitmq rabbitmqctl add_user "$NEW_MQ_USER" "$NEW_MQ_PASS"
docker compose exec rabbitmq rabbitmqctl set_permissions -p / "$NEW_MQ_USER" ".*" ".*" ".*"
docker compose exec rabbitmq rabbitmqctl set_user_tags "$NEW_MQ_USER" administrator
docker compose exec rabbitmq rabbitmqctl delete_user guest
# MinIO: create the new root-equivalent account with `mc admin user add`, or accept
# that MINIO_ROOT_* only takes effect on a fresh volume and keep the old pair in .env.
```

Option B is what the production runbook (WP-2.12) uses. Verify with
`docker compose config` first — it fails loudly if any required variable is unset.

Notes:
- `output/` is a bind mount on the `ui` service and must be writable by uid 10003;
  `.dockerignore` excludes it from build contexts by design.
- The UI image is the THIN client (`Dockerfile.ui`, ~570 MB — pyarrow is a hard
  Streamlit dependency); extraction always runs in the `worker` image.
- Local dev without Docker: `venv/Scripts/streamlit run src/hitl/app.py`
  (in-process extraction; unset `FINALZE_API_URL`).
- **Anything that imports `api.settings` needs `FINALZE_SECRET_KEY`** — it has no
  default (a default signing secret means forgeable admin tokens), and
  `get_settings()` runs at import time in ten modules, so the API, the worker and
  the migration runner all refuse to start without it. Put it in a local `.env` for
  development; the test suite sets a throwaway value in `tests/conftest.py`. The UI
  image does not import it.
- `.env.example` documents only variables that actually reach a container. If you add
  a `FINALZE_*` setting, wire it through the `x-app-env` anchor in the same commit or
  leave it out of the template — an inert variable there reads as configuration and
  silently does nothing.

### Production rebuild (runbook)

`docker-compose.yml` **bakes `src/` into the api, worker and ui images** (`COPY src/
/app/src/` in each Dockerfile), so a `restart` — or a plain `up` — changes no code. The
only way merged work reaches the server is a rebuild. `scripts/rebuild_prod.sh` is that
rebuild written down: the same twelve steps, each printing what it expects.

**Prerequisites** (all on the server, before you start):

- **Firewall** (decision G-3 option A) — inbound `8000/tcp` restricted at the cloud
  provider to the owner's IP. Docker-published ports bypass `ufw`, so the rule has to
  live at the provider, not on the host.
- **Nothing else is reachable.** The review UI (8501), Flower (5555), RabbitMQ
  management (15672) and the MinIO console (9001) are bound to `127.0.0.1`; reach them
  with `ssh -L 8501:127.0.0.1:8501 user@host`. The smoke script probes both
  `https://127.0.0.1:8000` and `http://127.0.0.1:8501`, so it must run **on** the host.
- **`.env`**, filled in — every credential is a `${VAR:?}` in `docker-compose.yml` and
  `:?` fires on *empty* as well as unset, so a verbatim copy of `.env.example` cannot
  boot a stack. A UTF-8 BOM is harmless: compose strips it and `src/api/settings.py`
  reads the file as `utf-8-sig` (so does the script's `.env` reader).
- **Certificate + key** — `TRAEFIK_CERT_FILE` / `TRAEFIK_KEY_FILE` pointing at real
  *files* (`scripts/gen_selfsigned_cert.sh <host>`, or the bank PEM; leaf first if it
  arrived as leaf + intermediate). `chmod 700 certs && chmod 600 certs/server.key`.
- **The working tree on the ref you mean to ship, and clean** — the images are built
  from it.

```bash
# 0. Rehearse. Changes nothing; prints the plan and every command it would run.
#    Preflight problems show as "WOULD ABORT" instead of stopping, so this also works
#    from your workstation.
scripts/rebuild_prod.sh --dry-run

# 1. The rebuild. --yes is required for any real run. Pick ONE upgrade path (OPS-15):
scripts/rebuild_prod.sh --yes --rotated          # keep the data — option B above was applied
scripts/rebuild_prod.sh --yes --reset-volumes    # DEV/STAGING ONLY — destroys the data

# Options: --verify-migrations (STAGING ONLY, destructive — see below) · --no-smoke ·
#          --trusted-cert (drop the smoke's --insecure) · --ref <git-ref>
```

**The steps, and what "good" looks like**

| # | Step | Expected output |
|---|---|---|
| 1 | repo ref + clean tree | `OK on feat/hitl-ui-flags`, `OK working tree clean` |
| 2 | `.env` + `docker compose config -q` | exits 0 and prints **nothing** |
| 3 | cert/key are files | `OK TRAEFIK_CERT_FILE -> …`, same for the key |
| 4 | daemon + disk | `OK docker daemon <version>`, `OK <n> GB free` |
| 5 | upgrade path (OPS-15) | `OK --rotated: …`, or the `*** DATA LOSS ***` banner + `down -v` |
| 6 | `docker compose build --pull` | every service image rebuilt |
| 7 | `docker compose up -d` | the nine services created/recreated (no `gpu` profile) |
| 8 | api healthy | `OK api healthy after <n>s` |
| 9 | schema + data checks | `OK database at audit_target_index_0004 (head)`, `OK uq_variation_candidate_org_raw_matched is present`, `OK no NULL-org …` |
| 10 | `verify_migrations.sh` | skipped unless `--verify-migrations` |
| 11 | smoke | `lockdown OK — 12 internal paths 404, 2 health paths 200` … `ALL CHECKS PASSED` |
| 12 | post | `docker compose ps`, the tunnel commands, the prune suggestion |

Step 9 is the OPS-1/OPS-2 exit check. `python -m api.migrate` runs inside the api
container between `api.bootstrap` and uvicorn and **decides any stamp by inspecting the
schema** (the three cases are tabulated in `alembic/versions/README.md`); today's prod is
case (c), where the stamp is deliberately one revision behind the head and the following
`upgrade head` is what applies `audit_target_index_0004`. The script asserts `alembic
current` contains `<head> (head)` and that `alembic heads` names exactly one head.

**Two checks that print a notice and are never applied automatically**

- **OPS-14** — a volume that took case (b) had `improvement_loop_0002` *stamped*, which
  marks `varcand_unique_0001` applied without running it, so
  `uq_variation_candidate_org_raw_matched` may be missing; the HITL upsert in
  `src/api/routers/review.py` depends on it. The script prints the exact SQL (collapse
  duplicates, then `ALTER TABLE … ADD CONSTRAINT`) for a human to run after a dump. It
  deletes rows, so nothing here runs it for you.
- **NULL-org rows** — the `Issue` / `RemediationAction` org filters are unconditional, so
  improvement-loop rows written before org scoping become invisible after the rebuild.
  The script counts them and prints the `UPDATE … SET org_id` to re-home them.

**`--verify-migrations` is a staging flag.** `scripts/verify_migrations.sh --yes` starts
with `docker compose down -v`: it *deletes* this project's `pgdata`, `rabbitmqdata` and
`miniodata` volumes to drive a fresh and a simulated pre-`dc1016c` volume through a
container start. Never point it at the live host. It also connects as `psql -U finalze -d
finalze`; since credentials moved into `.env` that pair is whatever you chose, and on a
deployment that is not `finalze/finalze` its psql checks fail for that reason alone —
`rebuild_prod.sh` warns when it detects the mismatch.

**Failure signatures**

| What you see | What it means |
|---|---|
| `invalid interpolation format` / `required variable … is missing` from `docker compose config` | that variable is **unset or empty** in `.env`. `:?` fires on empty too; the message names the first offender |
| postgres/rabbitmq authentication failures right after `up`, api never healthy | OPS-15: the accounts *inside* the existing volumes are still the old ones. You needed `--rotated` (having actually rotated them) or `--reset-volumes` |
| `TLSV1_ALERT_INTERNAL_ERROR` / handshake aborted on 8000 | `TRAEFIK_CERT_FILE` resolved to a **directory**. Docker Desktop creates one where a bind source is missing (it ignores `create_host_path:false`). Delete it and repoint the variable |
| smoke prints `traefik is serving its built-in fallback certificate` | the TLS store did not load — the cert bind or the `tls:` section of `traefik/dynamic.yml` is not reaching the container. Traefik degrades to its own cert *silently*; this probe is the only thing that catches it |
| clients reject the certificate although traefik starts | a bank cert delivered as leaf + separate intermediate was not concatenated **leaf first**. Traefik serves an incomplete chain without complaining |
| `alembic current` prints **nothing** | the migrate step never completed — the api container exited before uvicorn. `docker compose logs --tail 60 api` has the real error (usually a missing `FINALZE_SECRET_KEY` or an alembic failure) |
| `expected exactly one alembic head, got 2` | the revision chain branched. Merge the revisions before rebuilding — "the head" is ambiguous until you do |
| smoke: `FAIL: host lockdown breached` | an internal path answered something other than 404 through the public port, or a health path did not answer 200. The lines below it name the paths. Do **not** ship past this |
| smoke: `FAIL: set FINALZE_ADMIN_PASSWORD` | `.env` was not readable by the script (or the variable is empty) — export `FINALZE_ADMIN_EMAIL`/`FINALZE_ADMIN_PASSWORD` yourself and re-run |
| the script exits **3** | existing data volumes and no upgrade path chosen. That is the OPS-15 refusal, not a bug |
| the script exits **2** | bad usage — most often a real run without `--yes` |

The smoke submits `Samples/Bilan 1.jpg` through the real worker and leaves **one
validated document with one correction** behind; that write is the P2 exit check
(`POST …/review` → 200), not an accident. Only once you are satisfied with the new stack,
reclaim the superseded layers with `docker image prune -f` — the script suggests it and
never runs it.

### Learning log (`FINALZE_VARIATION_LOG`)

Every run logs fuzzy/low-confidence label matches to a JSONL learning log. That log is
**never a tracked repo file** — it records which banks a client borrows from, so it is
written per tenant into an untracked directory. The path is resolved at each
`VariationLogger(...)` instantiation, in this order: an explicit `log_path=` argument,
then `FINALZE_VARIATION_LOG`, then `data/variation_candidates/<org_id or "default">.jsonl`.
`FINALZE_VARIATION_LOG` is a *file* path; a literal `{org_id}` in it is substituted with
the tenant id (or `default`), which is how compose points every container at
`/app/data/variation_candidates/{org_id}.jsonl` on the shared `finalze_data` volume.
Set it before running any probe, sweep or benchmark so the run cannot write into the repo.

### Non-public documents in the benchmark (GOV-3 / GOV-1)

Five of the sixteen corpus documents are client financial statements. Both extraction-
benchmark stages that hand a case to an external AI refuse every case that is not marked
`public: true` in the corpus manifest `tools/golden/corpus.yaml` — `reference` (the
oracle reads the source document) and `judge` (the candidate it reads carries the raw
extracted text). Refused cases are marked in the stage's `_tasks.json`, never become
`pending`/`ready` or `next`, and the stage exits 2. The check fails closed (an id the
manifest does not list is refused too) and is a manifest lookup, never a filename
heuristic. Ground truth and review for a refused case come from people, not from an AI. GOV-1 is closed by
record: decision G-2 A — the owner confirmed the five documents are authorised to be
tracked in git (including on private GitHub), so there is no history purge and `Samples/`
stays tracked. Session-level handling rules are in the repo-root `CLAUDE.md`.

## Accounting Standards

- **NCT** — Normes Comptables Tunisiennes: the only standard supported end to end (nomenclature, canonical
  matching, classification, validation rules).
- **IFRS** and **SYSCOHADA** — OCR spelling vocabulary and one validation-rule toggle only; the UI/API still
  offer them but run the NCT engine. See `context/Accounting-Standards-Handling.md` and Master-Plan item DOC-1.

## Tech Stack

| Component | Library |
|-----------|---------|
| DOCX Processing | Marker |
| Table Extraction | Docling + TableFormer |
| OCR Engine | EasyOCR (via Docling) |
| Layout Analysis | Surya |
| DataFrame Validation | Pandera |
| Record Validation | Pydantic |
| HITL UI | Streamlit |

## Phase 0 Status

- [ ] Project structure created
- [ ] Dependencies installed
- [ ] Sample document processing verified
- [ ] Pandera schemas defined
- [ ] Document type classifier prototype

---

**Version**: 0.1.0  
**Status**: Phase 0 — Setup
