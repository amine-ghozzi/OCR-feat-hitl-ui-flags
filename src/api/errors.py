"""RFC 9457 `application/problem+json` error handlers.

Overrides the default HTTPException + validation handlers so every error is a problem document
(`type`, `title`, `status`, `detail`, `instance`, `request_id`). No native FastAPI support — hand-rolled.
"""

from __future__ import annotations

import logging
from http import HTTPStatus

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)
PROBLEM_JSON = "application/problem+json"


def _title(code: int) -> str:
    try:
        return HTTPStatus(code).phrase
    except ValueError:
        return "Error"


def _problem(request: Request, status_code: int, detail, **extra) -> JSONResponse:
    content = {
        "type": "about:blank",
        "title": _title(status_code),
        "status": status_code,
        "detail": detail if isinstance(detail, str) else jsonable_encoder(detail),
        "instance": request.url.path,
        "request_id": getattr(request.state, "request_id", None),
        **extra,
    }
    return JSONResponse(status_code=status_code, media_type=PROBLEM_JSON, content=content)


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return _problem(request, exc.status_code, exc.detail)


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return _problem(request, 422, "Request validation failed", errors=jsonable_encoder(exc.errors()))


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s", request.url.path)
    return _problem(request, 500, "An unexpected error occurred")


def register_error_handlers(app) -> None:
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
