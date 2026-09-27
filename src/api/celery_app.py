"""Thin Celery *client* used by the API to enqueue work and read task state.

The API process must stay slim (no torch/docling), so it never imports the worker or the
pipeline — it publishes by **task name** to the shared broker. The worker
(``src/worker``) registers and executes the task.
"""

from __future__ import annotations

from celery import Celery

from api.settings import get_settings

_settings = get_settings()

RUN_EXTRACTION = "worker.tasks.run_extraction"
DEFAULT_QUEUE = "ocr.default"

celery_client = Celery(
    "finalze-client",
    broker=str(_settings.broker_url),
    backend=str(_settings.result_backend_url),
)
celery_client.conf.update(
    task_default_queue=DEFAULT_QUEUE,
    task_routes={RUN_EXTRACTION: {"queue": DEFAULT_QUEUE}},
    task_track_started=True,
)


def enqueue_extraction(job_id: str) -> str:
    """Publish an extraction job; return the Celery task id (stored on the Job row)."""
    result = celery_client.send_task(RUN_EXTRACTION, args=[job_id], queue=DEFAULT_QUEUE)
    return result.id


def get_task_state(task_id: str) -> str:
    """Coarse Celery state (PENDING/STARTED/SUCCESS/FAILURE). Source of truth is the Job row."""
    return celery_client.AsyncResult(task_id).state
