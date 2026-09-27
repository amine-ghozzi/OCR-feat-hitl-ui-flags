"""Pydantic API DTOs (pure — no torch). fastapi-users user schemas + partner API-key schemas.

Extraction-result DTOs (Phase 4) serialize ``ExtractionResult`` via ``dataclasses.asdict()`` +
per-table ``CanonicalTable.to_dict()`` and live alongside these.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi_users import schemas
from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.security import ALL_SCOPES


# --- Users (extend fastapi-users base schemas) ---------------------------
class UserRead(schemas.BaseUser[uuid.UUID]):
    role: str = "analyst"
    org_id: uuid.UUID | None = None
    full_name: str | None = None


class UserCreate(schemas.BaseUserCreate):
    """Fields a *requester* may supply. `role` and `org_id` are NOT among them (SEC-1/SEC-3):
    there is no self-registration, and an admin's create sets both server-side from the caller's
    principal (see `routers/admin_users.py`)."""
    full_name: str | None = None


class UserUpdate(schemas.BaseUserUpdate):
    """Self-service profile edits. `role` is absent on purpose (SEC-15): fastapi-users' own
    ``PATCH /users/me`` uses ``create_update_dict()``, which strips only
    ``is_superuser``/``is_active``/``is_verified`` — a `role` field here would let any
    authenticated user promote itself to `admin` via `scopes_for_user`. Role changes go
    through the org-scoped ``PATCH /api/v1/users/{user_id}/role`` instead.

    Note this schema is now reached only through fastapi-users' ``GET /users/me``-adjacent
    plumbing: ``PATCH /users/me`` is served by `routers/admin_users.py`, which re-authenticates
    before an email or password change."""
    full_name: str | None = None


# --- Partner API keys -----------------------------------------------------
class ApiKeyCreate(BaseModel):
    name: str
    scopes: list[str] = []
    expires_at: datetime | None = None

    @field_validator("scopes")
    @classmethod
    def _known_scopes(cls, v: list[str]) -> list[str]:
        """A key may only carry scopes the system defines (SEC-4) → 422 otherwise."""
        unknown = sorted(set(v) - ALL_SCOPES)
        if unknown:
            raise ValueError(
                f"Unknown scope(s): {unknown}. Allowed: {sorted(ALL_SCOPES)}"
            )
        return v


class ApiKeyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    active: bool
    expires_at: datetime | None = None
    created_at: datetime


class ApiKeyCreated(ApiKeyRead):
    """Returned only at mint time — carries the one-time plaintext key."""
    api_key: str


# --- Documents & jobs -----------------------------------------------------
class SubmitResponse(BaseModel):
    job_id: uuid.UUID
    document_id: uuid.UUID
    status: str


class JobSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    status: str
    tier_used: int | None = None
    qcs_score: float | None = None
    hitl_required: bool = False
    progress: list[dict] = Field(default_factory=list)
    error: str | None = None


class DocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    filename: str
    accounting_standard: str
    state: str
    entity: str | None = None
    period: str | None = None
    created_at: datetime


class DocumentDetail(DocumentRead):
    latest_job: JobSummary | None = None


class PageResult(BaseModel):
    items: list[DocumentRead]
    limit: int
    offset: int
    total: int


# --- HITL review / validation --------------------------------------------
class ReviewQueueItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    filename: str
    state: str
    accounting_standard: str
    created_at: datetime
    hitl_required: bool = False
    qcs_score: float | None = None


class ValidationView(BaseModel):
    document_id: uuid.UUID
    state: str
    version_id: int
    assignee_id: uuid.UUID | None = None
    report: dict | None = None
    conflicts: list = []
    flagged_cells: list = []


class CellCorrection(BaseModel):
    table_index: int | None = None
    row_index: int | None = None
    column_name: str | None = None
    raw_text: str | None = None
    old_value: str | None = None
    new_value: str | None = None
    corrected_canonical: str | None = None


class CellsPatch(BaseModel):
    corrections: list[CellCorrection]


class CellsPatchResult(BaseModel):
    applied: int
    variation_candidates: int
    validation_version: int


class TransitionRequest(BaseModel):
    to_state: str            # validated | archived | rejected
    reason: str | None = None


class AssignRequest(BaseModel):
    assignee_id: uuid.UUID | None = None  # None → claim to self


class AuditEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    action: str
    actor_kind: str
    actor_id: uuid.UUID | None = None
    detail: dict | None = None
    created_at: datetime


# --- Feedback loop --------------------------------------------------------
class VariationCandidateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    raw_text: str
    matched_to: str | None = None
    confidence: float | None = None
    match_type: str | None = None
    status: str
    seen_count: int
    created_at: datetime


class QualityKPIs(BaseModel):
    jobs_total: int
    jobs_done: int
    automation_rate: float
    hitl_rate: float
    avg_qcs: float | None = None
    corrections_total: int
    correction_rate: float
    # Variation-candidate health (Layer 3 detection): duplication should stay ~0 under
    # the unique constraint; qualifier mismatches flag residual/total false matches.
    candidate_duplication_ratio: float = 0.0
    candidate_empty_document_id_pct: float = 0.0
    candidate_qualifier_mismatches: int = 0


class RuleFlagStats(BaseModel):
    jobs_evaluated: int
    avg_pass_rate: float | None = None
    critical_failure_rate: float


# --- Config ---------------------------------------------------------------
class OrgConfigRead(BaseModel):
    org_id: uuid.UUID
    config: dict


class OrgConfigUpdate(BaseModel):
    config: dict


class StandardInfo(BaseModel):
    code: str
    name: str


class NomenclatureTerm(BaseModel):
    canonical_term: str
    statement_type: str
    account_code: str | None = None


# --- Webhooks -------------------------------------------------------------
class WebhookCreate(BaseModel):
    url: str
    events: list[str] = []


class WebhookRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    url: str
    events: list[str]
    active: bool
    created_at: datetime


class WebhookCreated(WebhookRead):
    secret: str  # shown once at creation


class WebhookDeliveryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    endpoint_id: uuid.UUID
    event: str
    status_code: int | None = None
    attempts: int
    delivered: bool
    created_at: datetime


# --- Self-improving control loop -------------------------------------------


class IssueRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    kind: str
    severity: str
    status: str
    title: str
    evidence: dict | None = None
    fingerprint: str
    created_at: datetime


class SweepResult(BaseModel):
    detected: int
    created: int
    issues: list[IssueRead]


class RemediationActionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    issue_id: uuid.UUID
    action_type: str
    status: str
    proposal: dict | None = None
    before: dict | None = None
    after: dict | None = None
    gate_result: dict | None = None


class ProposalRead(BaseModel):
    issue_id: uuid.UUID
    action_type: str
    reversible: bool
    rationale: str
    proposal: dict


class ApplyRequest(BaseModel):
    action_type: str                      # currently only "config_tune" is auto-appliable
    config: dict | None = None            # for config_tune: the new OrgConfig.config
