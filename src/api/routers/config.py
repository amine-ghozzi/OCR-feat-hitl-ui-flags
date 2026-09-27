"""Per-org configuration + reference data (standards, nomenclature).

GET config/standards/nomenclature need `documents:read`; PUT config needs `admin`.
Lets partners tune thresholds/standard without a redeploy and discover the canonical schema.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from api.db import get_async_session
from api.deps import audit, tenant_org_id
from api.models import OrgConfig
from api.schemas import (
    NomenclatureTerm,
    OrgConfigRead,
    OrgConfigUpdate,
    StandardInfo,
)
from api.security import Principal, require_scopes

router = APIRouter(tags=["config"])

# NCT first — the only fully-supported standard (canonical nomenclature +
# validation). IFRS/SYSCOHADA apply standard-specific OCR term corrections only;
# see context/Accounting-Standards-Handling.md.
_STANDARDS = [
    ("NCT", "Normes Comptables Tunisiennes"),
    ("IFRS", "International Financial Reporting Standards"),
    ("SYSCOHADA", "Système Comptable OHADA"),
]


@router.get("/config", response_model=OrgConfigRead)
async def get_config(
    principal: Principal = Depends(require_scopes("documents:read")),
    session: AsyncSession = Depends(get_async_session),
) -> OrgConfigRead:
    org = tenant_org_id(principal)
    cfg = await session.get(OrgConfig, org)
    return OrgConfigRead(org_id=org, config=cfg.config if cfg else {})


@router.put("/config", response_model=OrgConfigRead)
async def put_config(
    body: OrgConfigUpdate,
    principal: Principal = Depends(require_scopes("admin")),
    session: AsyncSession = Depends(get_async_session),
) -> OrgConfigRead:
    org = tenant_org_id(principal)
    cfg = await session.get(OrgConfig, org)
    before = dict(cfg.config) if cfg is not None else None
    if cfg is None:
        cfg = OrgConfig(org_id=org, config=body.config)
        session.add(cfg)
    else:
        cfg.config = body.config
    # Thresholds decide what reaches a human: a config change is an auditable act. Partner keys
    # may make it (see this module's docstring), hence the actor may legitimately be a key.
    await audit(session, principal, "config.update", None,
                {"keys": sorted(body.config), "had_previous": before is not None},
                target_type="org_config", target_id=str(org))
    await session.commit()
    return OrgConfigRead(org_id=org, config=cfg.config)


@router.get("/standards", response_model=list[StandardInfo])
async def list_standards(
    _: Principal = Depends(require_scopes("documents:read")),
) -> list[StandardInfo]:
    return [StandardInfo(code=c, name=n) for c, n in _STANDARDS]


@router.get("/nomenclature", response_model=list[NomenclatureTerm])
async def nomenclature(
    statement_type: str | None = None,
    _: Principal = Depends(require_scopes("documents:read")),
) -> list[NomenclatureTerm]:
    from accounting.nomenclature import load_default_dictionary  # noqa: PLC0415

    d = load_default_dictionary()
    entries = d.get_terms_for(statement_type) if statement_type else d.all_entries
    return [
        NomenclatureTerm(
            canonical_term=e.canonical_term, statement_type=e.statement_type,
            account_code=e.account_code,
        )
        for e in entries
    ]
