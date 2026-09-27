"""FinAlze backend API package.

A slim FastAPI service that wraps the OCR pipeline behind an authenticated HTTP API.
The API process holds **no ML dependencies** — it validates/stores uploads, enqueues
Celery tasks, and serves status/results/HITL. Heavy extraction runs in ``src/worker``.

See ``context/Vision-and-Architecture-Directions.md`` for the architecture and
``~/.claude/plans/scalable-petting-naur.md`` for the implementation plan.
"""
