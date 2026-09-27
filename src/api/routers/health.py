"""Liveness/readiness probes. Deep dependency checks (DB/Redis/RabbitMQ) land in Phase 5/7;
``/metrics`` is wired by the Prometheus instrumentator (Phase 5)."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["ops"])


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> dict:
    return {"status": "ready"}
