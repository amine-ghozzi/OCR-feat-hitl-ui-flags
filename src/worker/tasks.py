"""The extraction task. Sync (prefork) body; DB writes via ``asyncio.run`` on fresh engines.

Failure is persisted only after retries are exhausted; intermediate errors re-raise so Celery
retries with backoff. A missing job is skipped cleanly via ``Ignore`` (e.g. cancelled/deleted).
"""

from __future__ import annotations

import asyncio
import logging

from celery.exceptions import Ignore, SoftTimeLimitExceeded

from worker import runtime
from worker.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="worker.tasks.run_extraction",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=3,
    acks_late=True,
)
def run_extraction(self, job_id: str) -> dict:
    ctx = asyncio.run(runtime.load_job_context(job_id))
    if ctx is None:
        logger.warning("Job %s not found (cancelled/deleted?) — skipping.", job_id)
        raise Ignore()

    from document_intake import DocumentRejected, event
    events = []
    def emit(item):
        events.append(item)
        asyncio.run(runtime.save_progress(job_id, events))

    try:
        emit(event("loading", "running", "Chargement du document et du moteur"))
        path = runtime.fetch_document(ctx["storage_key"], ctx["filename"])
        pipeline = runtime.get_pipeline()
        runtime.configure_pipeline(pipeline, ctx["standard"], ctx.get("org_config"))
        result = pipeline.process_document(path, accounting_standard=ctx["standard"],
                                           progress_callback=emit)
        payload = runtime.serialize_result(result)
        hitl = bool((result.metadata or {}).get("hitl_required", False))
        asyncio.run(
            runtime.save_success(job_id, payload, result.tier_used, result.qcs_score, hitl)
        )
        logger.info("Job %s done (tier=%s, qcs=%.3f)", job_id, result.tier_used, result.qcs_score)
        return {"job_id": job_id, "status": "done",
                "tier_used": result.tier_used, "qcs": result.qcs_score}
    except DocumentRejected as exc:
        emit(event("admissibility", "rejected", str(exc), report=exc.report))
        asyncio.run(runtime.save_failure(job_id, exc))
        return {"job_id": job_id, "status": "failed", "rejection": exc.report}
    except SoftTimeLimitExceeded:
        asyncio.run(runtime.save_failure(job_id, "soft time limit exceeded"))
        raise
    except Exception as exc:  # noqa: BLE001
        # Persist failure only on the final attempt; otherwise let Celery retry.
        if self.request.retries >= self.max_retries:
            asyncio.run(runtime.save_failure(job_id, exc))
        raise
