# FinAlze Backend — E2E Quickstart

Bring up the full stack and exercise the API end-to-end.

## Run

```bash
cp .env.example .env            # then edit FINALZE_SECRET_KEY (openssl rand -hex 32)
docker compose up -d --build    # postgres, redis, rabbitmq, minio, api, worker, flower, traefik
# Tier-2 GPU worker (later): docker compose --profile gpu up -d
```

| Service | URL |
|---|---|
| API (via Traefik) | http://localhost:8000  · docs at `/docs` |
| Flower (Celery) | http://localhost:5555 |
| RabbitMQ mgmt | http://localhost:15672 (guest/guest) |
| MinIO console | http://localhost:9001 (minioadmin/minioadmin) |
| Traefik dashboard | http://localhost:8080 |

The API container runs `python -m api.bootstrap && python -m api.migrate` on start (`Dockerfile.api`,
before uvicorn). `bootstrap` creates tables (`create_all`) and an admin from
`FINALZE_ADMIN_EMAIL`/`FINALZE_ADMIN_PASSWORD`; `api.migrate` then brings the database to the Alembic
head, **deciding any stamp by inspecting the live schema** — a bare `alembic upgrade head` fails on a
`create_all` volume, which has no `alembic_version` table. The `&&` is load-bearing: a failed migration
exits the container instead of serving. **Production is the same command** — nothing is stamped or
upgraded by hand. Inspect without changing anything with `python -m api.migrate --dry-run`; the three
cases and the known gap are in `alembic/versions/README.md`, and README "Production rebuild (runbook)"
is the deployment sequence.

## Smoke (curl)

```bash
BASE=http://localhost:8000/api/v1

# 1) Admin login → token
TOKEN=$(curl -s -X POST "$BASE/auth/jwt/login" \
  -d "username=admin@finalze.io&password=changeme-admin-pw" | jq -r .access_token)

# 2) Submit a sample document
RESP=$(curl -s -X POST "$BASE/documents" -H "Authorization: Bearer $TOKEN" \
  -F "file=@Samples/Bilan 1.jpg" -F "accounting_standard=NCT")
DOC=$(echo "$RESP" | jq -r .document_id)

# 3) Poll status, then fetch the result
curl -s "$BASE/documents/$DOC" -H "Authorization: Bearer $TOKEN" | jq .latest_job.status
curl -s "$BASE/documents/$DOC/result" -H "Authorization: Bearer $TOKEN" | jq .

# 4) HITL queue + feedback KPIs
curl -s "$BASE/review/queue" -H "Authorization: Bearer $TOKEN" | jq .
curl -s "$BASE/feedback/quality" -H "Authorization: Bearer $TOKEN" | jq .
```

### Partner (API key) path

```bash
# Mint a key for a partner org (admin)
ORG=$(curl -s -X POST "$BASE/partners" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"name":"Partner Co"}' | jq -r .id)
KEY=$(curl -s -X POST "$BASE/partners/$ORG/api-keys" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"ingest","scopes":["documents:submit","documents:read"]}' | jq -r .api_key)

# Use it
curl -s -X POST "$BASE/documents" -H "X-API-Key: $KEY" \
  -F "file=@Samples/Bilan 1.jpg" -F "accounting_standard=NCT"
```

## Python SDK / CLI

```python
from client.sdk import FinAlzeClient
with FinAlzeClient("http://localhost:8000", api_key=KEY) as c:
    print(c.submit_and_wait("Samples/Bilan 1.jpg", accounting_standard="NCT"))
```
```bash
python src/pipeline.py -i "Samples/Bilan 1.jpg" -s NCT --remote http://localhost:8000 --api-key "$KEY"
```

The Streamlit HITL UI uses the API when `FINALZE_API_URL` is set (else it runs the pipeline locally).
