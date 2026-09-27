"""SQLAlchemy ORM models — operational store for the FinAlze backend.

Tenant-scoped tables carry ``org_id``. The analytical "gold" layer (KG / warehouse)
is a *separate* store fed by ``document.validated`` events — not modeled here.

Notes:
- UUID primary keys (``sqlalchemy.Uuid`` — cross-dialect in 2.0).
- ``Validation`` uses **optimistic locking** (``version_id_col``) → ``StaleDataError`` on
  concurrent HITL edits → surfaced as HTTP 409.
- JSON columns use the generic ``JSON`` type so the same models run on SQLite (tests)
  and Postgres (prod, stored as JSONB by the dialect).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from fastapi_users.db import SQLAlchemyBaseUserTableUUID
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.db import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


class DocState(str, enum.Enum):
    draft = "draft"          # extracted, awaiting review
    validated = "validated"  # HITL-approved
    archived = "archived"
    rejected = "rejected"


class VariationStatus(str, enum.Enum):
    candidate = "candidate"
    approved = "approved"
    rejected = "rejected"
    promoted = "promoted"          # written into the Nomenclature
    confirmed_custom = "confirmed_custom"


class IssueStatus(str, enum.Enum):
    open = "open"            # detected by a sweep, not yet acted on
    proposed = "proposed"    # a remediation proposal exists
    applied = "applied"      # a reversible fix was applied (gated)
    dismissed = "dismissed"  # human/loop decided no action
    resolved = "resolved"    # confirmed fixed


class RemediationStatus(str, enum.Enum):
    proposed = "proposed"
    applied = "applied"
    rolled_back = "rolled_back"
    failed = "failed"        # gate rejected / apply error


# ---------------------------------------------------------------------------
# Tenancy & identity
# ---------------------------------------------------------------------------


class Org(Base, TimestampMixin):
    __tablename__ = "orgs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class User(SQLAlchemyBaseUserTableUUID, Base):
    """fastapi-users base (email, hashed_password, is_active/superuser/verified) + extras."""
    __tablename__ = "users"
    org_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orgs.id"), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(50), default="analyst", nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)


class ApiKey(Base, TimestampMixin):
    """Partner machine-to-machine key. Plaintext shown once at mint; only the hash is stored."""
    __tablename__ = "api_keys"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    prefix: Mapped[str] = mapped_column(String(12), index=True, nullable=False)  # fast lookup hint
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False)           # Argon2 (pwdlib)
    scopes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OrgConfig(Base, TimestampMixin):
    """Per-org tunables (thresholds, default standard, limits) — editable without redeploy."""
    __tablename__ = "org_configs"
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"), primary_key=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


# ---------------------------------------------------------------------------
# Documents & jobs
# ---------------------------------------------------------------------------


class Document(Base, TimestampMixin):
    __tablename__ = "documents"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)   # MinIO/S3 object key
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    accounting_standard: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[DocState] = mapped_column(default=DocState.draft, nullable=False, index=True)
    # Analytical keys (forward seam for KG/warehouse ingestion)
    entity: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    period: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    jobs: Mapped[list["Job"]] = relationship(back_populates="document")


class Job(Base, TimestampMixin):
    __tablename__ = "jobs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    status: Mapped[JobStatus] = mapped_column(default=JobStatus.queued, nullable=False, index=True)
    tier_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    qcs_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    hitl_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # serialized ExtractionResult
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Lineage / reproducibility (hardening)
    pipeline_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    engine_versions: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    config_used: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    @property
    def progress(self) -> list:
        payload = self.result or {}
        return payload.get("progress") or (payload.get("metadata") or {}).get("workflow_events") or []

    document: Mapped["Document"] = relationship(back_populates="jobs")


# ---------------------------------------------------------------------------
# HITL: validations, corrections, audit
# ---------------------------------------------------------------------------


class Validation(Base, TimestampMixin):
    """HITL validation record per document (optimistic-locked for concurrent editing)."""
    __tablename__ = "validations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    version_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[DocState] = mapped_column(default=DocState.draft, nullable=False)
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    report: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # V1–V4 report snapshot
    quality: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Optimistic concurrency: SQLAlchemy appends version_id to the UPDATE WHERE clause and
    # raises StaleDataError on a stale/concurrent edit (→ surfaced as HTTP 409).
    __mapper_args__ = {"version_id_col": version_id}


class Correction(Base, TimestampMixin):
    """A single human cell/label correction (the feedback-loop atom)."""
    __tablename__ = "corrections"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    table_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Which result keying this correction was recorded against. v1 rows are
    # addressed by `table_index` alone; from v2 `canonical_tables` is keyed per
    # statement, so `statement_id` disambiguates. Nullable and defaulted so
    # existing rows keep their meaning without a backfill.
    statement_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    schema_version: Mapped[int | None] = mapped_column(Integer, nullable=True, default=1)
    row_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    column_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    corrected_canonical: Mapped[str | None] = mapped_column(String(255), nullable=True)


class VariationCandidate(Base, TimestampMixin):
    """Migrated from data/variation_candidates.jsonl → DB; promotable into the Nomenclature.

    A mapping is one row per ``(org_id, raw_text, matched_to)``; repeat confirmations
    increment ``seen_count`` via UPSERT rather than inserting duplicates (the write path
    in ``routers/review.py`` always supplies a non-null ``org_id``). Bulk-imported local
    candidates with null ``org_id`` are exempt by SQL NULL semantics and deduped at import.
    """
    __tablename__ = "variation_candidates"
    __table_args__ = (
        UniqueConstraint("org_id", "raw_text", "matched_to",
                         name="uq_variation_candidate_org_raw_matched"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    org_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orgs.id"), nullable=True, index=True)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    matched_to: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    match_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    section: Mapped[str | None] = mapped_column(String(128), nullable=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    seen_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[VariationStatus] = mapped_column(default=VariationStatus.candidate, nullable=False, index=True)


class AuditEvent(Base, TimestampMixin):
    """Immutable who-did-what trail for HITL and admin actions."""
    __tablename__ = "audit_events"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    org_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orgs.id"), nullable=True, index=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)  # user or api_key id
    actor_kind: Mapped[str] = mapped_column(String(16), default="user", nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


class WebhookEndpoint(Base, TimestampMixin):
    __tablename__ = "webhook_endpoints"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(255), nullable=False)  # HMAC secret (hashed at rest)
    events: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class WebhookDelivery(Base, TimestampMixin):
    __tablename__ = "webhook_deliveries"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    endpoint_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("webhook_endpoints.id"), nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Self-improving control loop (dev-time): detected issues + remediation ledger
# ---------------------------------------------------------------------------


class Issue(Base, TimestampMixin):
    """A quality issue detected by a sweep (signals.py) on the current build."""
    __tablename__ = "issues"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    org_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orgs.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)   # e.g. qualifier_mismatch
    severity: Mapped[str] = mapped_column(String(16), default="medium", nullable=False)
    status: Mapped[IssueStatus] = mapped_column(default=IssueStatus.open, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # signal payload (counts, samples)
    fingerprint: Mapped[str] = mapped_column(String(128), index=True, nullable=False)  # dedupe key


class RemediationAction(Base, TimestampMixin):
    """A planned/applied fix for an Issue, with before/after for rollback."""
    __tablename__ = "remediation_actions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    issue_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("issues.id"), nullable=False, index=True)
    org_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orgs.id"), nullable=True, index=True)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)  # config_tune|variation_promotion|code_fix_proposal
    status: Mapped[RemediationStatus] = mapped_column(default=RemediationStatus.proposed, nullable=False, index=True)
    proposal: Mapped[dict | None] = mapped_column(JSON, nullable=True)     # what to change
    before: Mapped[dict | None] = mapped_column(JSON, nullable=True)       # prior state (for rollback)
    after: Mapped[dict | None] = mapped_column(JSON, nullable=True)        # new state
    gate_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # regression_gate verdict
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
