"""Worker Celery app + reliability config (context7-validated).

Broker = RabbitMQ (durable, acks/redelivery, DLQ-capable); result backend = Redis.
Per-tier queues (`ocr.default` now, `ocr.gpu` reserved for Tier-2) so heavy GPU jobs never
block fast Tier-1 jobs. `acks_late` + `prefetch=1` + `reject_on_worker_lost` give
crash-safe redelivery; `soft_time_limit` < `task_time_limit` lets a long job clean up.
"""

from __future__ import annotations

from celery import Celery
from kombu import Exchange, Queue

from api.settings import get_settings

_settings = get_settings()

celery_app = Celery(
    "finalze",
    broker=str(_settings.broker_url),
    backend=str(_settings.result_backend_url),
    include=["worker.tasks"],
)

_ocr_exchange = Exchange("ocr", type="direct")

celery_app.conf.update(
    # --- routing / per-tier queues ---
    task_queues=(
        Queue("ocr.default", _ocr_exchange, routing_key="ocr.default"),
        Queue("ocr.gpu", _ocr_exchange, routing_key="ocr.gpu"),  # reserved: Tier-2 VLM
    ),
    task_default_queue="ocr.default",
    task_default_exchange="ocr",
    task_default_routing_key="ocr.default",
    task_routes={
        "worker.tasks.run_extraction": {"queue": "ocr.default", "routing_key": "ocr.default"},
    },
    # --- reliability ---
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    task_time_limit=1800,         # hard kill (s)
    task_soft_time_limit=1500,    # SoftTimeLimitExceeded → cleanup
    result_expires=86400,         # Redis result TTL (Postgres is source of truth)
    worker_max_tasks_per_child=20,  # recycle children to bound memory growth
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
)
