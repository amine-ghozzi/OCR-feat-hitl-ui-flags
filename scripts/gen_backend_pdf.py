"""Render the FinAlze backend capabilities document to a styled PDF (reportlab)."""
from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

OUT = Path(__file__).resolve().parents[1] / "FinAlze-Backend-Capabilities.pdf"

NAVY = colors.HexColor("#1a2b4a")
ACCENT = colors.HexColor("#2563eb")
GREY = colors.HexColor("#444444")
LIGHT = colors.HexColor("#f0f3f8")

ss = getSampleStyleSheet()
styles = {
    "title": ParagraphStyle("title", parent=ss["Title"], fontName="Helvetica-Bold",
                            fontSize=24, textColor=NAVY, spaceAfter=4, leading=28),
    "subtitle": ParagraphStyle("subtitle", parent=ss["Normal"], fontSize=11,
                               textColor=GREY, spaceAfter=2, leading=15),
    "h2": ParagraphStyle("h2", parent=ss["Heading2"], fontName="Helvetica-Bold",
                         fontSize=14, textColor=ACCENT, spaceBefore=14, spaceAfter=4, leading=18),
    "h3": ParagraphStyle("h3", parent=ss["Heading3"], fontName="Helvetica-Bold",
                         fontSize=10.5, textColor=NAVY, spaceBefore=6, spaceAfter=2, leading=13),
    "body": ParagraphStyle("body", parent=ss["Normal"], fontSize=9.5, textColor=GREY,
                          leading=14, spaceAfter=3, alignment=TA_LEFT),
    "bullet": ParagraphStyle("bullet", parent=ss["Normal"], fontSize=9.5, textColor=GREY,
                            leading=13, spaceAfter=2),
    "caveat": ParagraphStyle("caveat", parent=ss["Normal"], fontSize=9.5,
                            textColor=colors.HexColor("#8a4b00"), leading=13, spaceAfter=2),
    "footer": ParagraphStyle("footer", parent=ss["Normal"], fontSize=7.5,
                            textColor=colors.HexColor("#999999")),
}


def b(txt):
    return Paragraph(txt, styles["body"])


def bullets(items, style="bullet"):
    return ListFlowable(
        [ListItem(Paragraph(t, styles[style]), leftIndent=6,
                  value="•", bulletColor=ACCENT) for t in items],
        bulletType="bullet", leftIndent=10, spaceBefore=1, spaceAfter=4,
    )


def section(title):
    return [Paragraph(title, styles["h2"]),
            HRFlowable(width="100%", thickness=0.8, color=colors.HexColor("#cdd6e6"),
                       spaceBefore=1, spaceAfter=5)]


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(colors.HexColor("#999999"))
    canvas.drawString(20 * mm, 12 * mm, "FinAlze Backend — Capabilities")
    canvas.drawRightString(A4[0] - 20 * mm, 12 * mm, f"Page {doc.page}")
    canvas.setStrokeColor(colors.HexColor("#dddddd"))
    canvas.line(20 * mm, 15 * mm, A4[0] - 20 * mm, 15 * mm)
    canvas.restoreState()


story = []

# --- Header ---------------------------------------------------------------
story += [
    Paragraph("FinAlze Backend", styles["title"]),
    Paragraph("Full Capability Description", styles["subtitle"]),
    Paragraph("Multi-tenant, async, partner-facing financial-document extraction service",
              styles["subtitle"]),
    Spacer(1, 4),
    HRFlowable(width="100%", thickness=1.5, color=ACCENT, spaceAfter=8),
]

story += [b(
    "The backend turns the in-process OCR pipeline into a multi-tenant extraction service. "
    "A slim FastAPI API publishes jobs by name to a Celery / RabbitMQ broker; a separate worker "
    "(with the heavy torch / docling pipeline) executes them and writes results back to a shared "
    "operational store. The API never imports the pipeline, keeping it lightweight.")]

# --- 1 ---
story += section("1. Document Extraction (async job pipeline)")
story += [bullets([
    "<b>POST /api/v1/documents</b> — multipart upload &rarr; stored &rarr; extraction job enqueued; "
    "returns 202 with job_id / document_id. Streamed read with a size cap (default 25 MB) and an "
    "extension allowlist (pdf, docx, jpg/jpeg, png, tiff, bmp). Accepts an accounting_standard "
    "(IFRS / NCT / SYSCOHADA; default NCT).",
    "<b>Idempotency-Key</b> header dedupes partner retries — a repeat returns the original job.",
    "<b>GET /documents</b> — tenant-scoped, paginated list, filterable by state.",
    "<b>GET /documents/{id}</b> — detail with latest job summary (status, tier, QCS).",
    "<b>GET /documents/{id}/result</b> — full serialized ExtractionResult (text, tables, canonical "
    "tables, QCS, tier, confidence, metadata).",
    "<b>Worker task</b> — retry with exponential backoff (max 3), acks_late, soft-time-limit handling, "
    "clean skip for cancelled / deleted jobs; failure persisted only after retries exhaust. "
    "Pipeline lazy-loaded once per prefork child.",
])]

# --- 2 ---
story += section("2. Identity, Tenancy &amp; Authorization")
story += [bullets([
    "<b>Dual auth on one Principal</b> — human bearer token or partner X-API-Key resolve through the "
    "same dependency.",
    "<b>Human auth</b> via fastapi-users with a revocable Redis token strategy — logout deletes the "
    "server-side token (instant revocation), 30-min lifetime. Argon2 password hashing; policy "
    "(&ge;10 chars, no email-name).",
    "<b>Partner API keys</b> — one-time plaintext at mint; only an Argon2 hash stored, with a short "
    "indexed prefix for fast lookup before constant-time verify. Scopes + expiry + active flag.",
    "<b>Scope-based RBAC</b> via require_scopes(...). Roles &rarr; scopes (admin, analyst, viewer); "
    "scopes: documents:submit/read, hitl:write, feedback:write, analytics:read, admin.",
    "<b>Multi-tenancy</b> — every query scoped by org_id; cross-org access returns 404.",
])]

# --- 3 ---
story += section("3. HITL Review Workflow")
story += [bullets([
    "<b>GET /review/queue</b> — draft documents needing review, annotated with hitl_required + QCS.",
    "<b>GET /documents/{id}/validation</b> — validation view with V1–V4 report snapshot, derived "
    "conflicts and flagged (non-green) cells from the canonical tables.",
    "<b>POST /assign</b> — assign a reviewer.",
    "<b>PATCH /cells</b> — record human corrections as append-only rows; If-Match concurrency guard.",
    "<b>State machine (POST /transition)</b> — Draft &rarr; Validated / Rejected, Validated &rarr; Archived; "
    "illegal transitions rejected (409); optimistic locking (version_id) &rarr; 409 on concurrent edits.",
    "<b>Full audit trail</b> — every mutation writes an immutable AuditEvent; GET /audit returns it.",
])]

# --- 4 ---
story += section("4. Feedback Loop (compounding-accuracy engine)")
story += [bullets([
    "A HITL correction with a canonical mapping auto-seeds an approved VariationCandidate — closing "
    "the loop into the DB.",
    "<b>/feedback/variations</b> — list + approve / reject. Approval promotes and bridges into the "
    "pipeline's hot-reload VariationLogger so future extractions benefit.",
    "<b>/feedback/rules/flags</b> — validation-rule pass rates and critical-failure rate across jobs.",
    "<b>/feedback/quality</b> — KPIs: automation rate, HITL rate, avg QCS, correction rate, totals.",
    "<b>/feedback/ground-truth</b> — exports corrections as a JSONL labeled dataset (golden-set / "
    "calibration source).",
])]

# --- 5 ---
story += section("5. Configuration &amp; Reference Data")
story += [bullets([
    "<b>Per-org config</b> GET / PUT (thresholds, default standard, limits) — tunable without redeploy "
    "(PUT requires admin).",
    "<b>/standards</b> — IFRS / NCT / SYSCOHADA catalog.",
    "<b>/nomenclature</b> — canonical accounting term dictionary, filterable by statement type.",
])]

# --- 6 ---
story += section("6. Partner Administration")
story += [bullets([
    "Create partner orgs; mint API keys (one-time plaintext, scoped, optional expiry); revoke keys "
    "(soft-deactivate). All admin-scoped.",
])]

# --- 7 ---
story += section("7. Webhooks")
story += [bullets([
    "Register callback endpoints with an SSRF guard (rejects non-http(s), localhost, "
    "private / loopback / link-local / reserved IPs, .internal / .local). HMAC signing secret "
    "returned once, hashed at rest.",
    "List endpoints, list deliveries (observability), and replay a delivery. Actual signed delivery "
    "with retry / backoff is the worker's job on document.validated.",
])]

# --- 8 ---
story += section("8. Cross-cutting Platform")
story += [bullets([
    "RFC 9457 application/problem+json error envelopes everywhere, with request_id correlation.",
    "Request-ID middleware — accepts or mints X-Request-ID, echoed on response.",
    "Per-principal rate limiting (fastapi-limiter / Redis), bucketed by key / token / IP; degrades to "
    "no-op when Redis is absent; submit path tighter (30 / 60s).",
    "Prometheus /metrics (fresh registry per app); /healthz + /readyz probes.",
    "Object storage abstraction — MinIO / S3 when configured, local filesystem fallback for dev/tests.",
    "Typed settings via pydantic-settings (FINALZE_* env, .env, Docker/K8s /run/secrets), validated "
    "Postgres / Redis / RabbitMQ DSNs. CORS, docs gating, proxy root_path support.",
])]

# --- 9 ---
story += section("9. Data Model &amp; Lineage")
story += [b(
    "Org, User, ApiKey, OrgConfig, Document, Job, Validation, Correction, VariationCandidate, "
    "AuditEvent, WebhookEndpoint / Delivery. UUID PKs; JSON columns portable across SQLite (tests) "
    "and Postgres / JSONB (prod). Jobs carry lineage fields (pipeline_version, engine_versions, "
    "config_used) for reproducibility; Documents carry forward-seam analytical keys (entity, period) "
    "for future KG / warehouse ingestion.")]

# --- 10 ---
story += section("10. Deployment &amp; Clients")
story += [bullets([
    "Docker Compose stack: postgres, redis, rabbitmq, minio, api, worker, flower, traefik, optional "
    "GPU worker. Split requirements-api.txt / requirements-worker.txt.",
    "Startup (Dockerfile.api, before uvicorn): python -m api.bootstrap &amp;&amp; python -m api.migrate — "
    "idempotent create_all + initial superuser, then the Alembic head, with any stamp decided by "
    "inspecting the live schema (a bare `upgrade head` fails on a create_all volume). The &amp;&amp; is "
    "load-bearing: a failed migration exits the container instead of serving.",
    "Python SDK (client/sdk.py) — submit_and_wait(), both auth modes, injectable transport for tests; "
    "wired into the Streamlit UI and the pipeline CLI (--remote).",
])]

# --- Caveats ---
story += section("Maturity Caveats")
story += [ListFlowable(
    [ListItem(Paragraph(t, styles["caveat"]), leftIndent=6, value="!",
              bulletColor=colors.HexColor("#c87b00"))
     for t in [
        "<b>Alembic sits on top of a create_all baseline</b> — four revisions exist "
        "(varcand_unique_0001 … audit_target_index_0004) and api.migrate stamps by inspection "
        "rather than assuming the head. A volume that took the 'no version table, no "
        "corrections.statement_id' case is stamped at improvement_loop_0002, so 0001's "
        "uq_variation_candidate_org_raw_matched is never created on it (OPS-14, checked by "
        "scripts/rebuild_prod.sh).",
        "<b>Webhook signed delivery + retry</b> is referenced as the worker's responsibility but is "
        "not yet implemented (only config / observability endpoints + the extraction task exist).",
        "<b>Upload hardening</b> is allowlist + size-cap only; AV / content-sniffing is a documented "
        "follow-up.",
     ]],
    bulletType="bullet", leftIndent=10, spaceBefore=1, spaceAfter=4)]

story += [Spacer(1, 10),
          HRFlowable(width="100%", thickness=0.8, color=colors.HexColor("#cdd6e6"), spaceAfter=4),
          Paragraph("Generated from source review of src/api, src/worker, and src/client.",
                    styles["footer"])]

doc = SimpleDocTemplate(
    str(OUT), pagesize=A4,
    leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=20 * mm,
    title="FinAlze Backend — Capabilities", author="FinAlze",
)
doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
print(f"Wrote {OUT} ({OUT.stat().st_size:,} bytes)")
