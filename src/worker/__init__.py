"""FinAlze Celery worker package.

Holds the heavy extraction stack (torch/docling/VLM). The worker lazy-loads
``FinAlzePipeline`` once per prefork child (never in ``worker_process_init`` — that handler
is killed if it blocks >4 s, and model load exceeds that). Run with:

    celery -A worker.celery_app worker -Q ocr.default --pool=prefork --concurrency=1

A second GPU worker on ``-Q ocr.gpu`` is added when Tier-2 VLM lands (Compose ``gpu`` profile).
"""
