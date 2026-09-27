"""Per-principal rate limiting (fastapi-limiter, Redis) that degrades gracefully.

The custom identifier buckets by API key / bearer token / client IP. When Redis isn't
initialized (dev/tests, or Redis down), the limiter **no-ops** so the API still serves.
"""

from __future__ import annotations

import logging

import redis.asyncio as redis_asyncio
from starlette.requests import Request
from starlette.responses import Response

from api.settings import get_settings

logger = logging.getLogger(__name__)
_settings = get_settings()

try:
    from fastapi_limiter import FastAPILimiter
    from fastapi_limiter.depends import RateLimiter

    _AVAILABLE = True
except Exception:  # noqa: BLE001
    _AVAILABLE = False


async def _identifier(request: Request) -> str:
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return "k:" + api_key[:16]
    auth = request.headers.get("Authorization")
    if auth:
        return "t:" + auth[-16:]
    return "ip:" + (request.client.host if request.client else "anon")


async def init_limiter() -> None:
    if not _AVAILABLE:
        return
    try:
        client = redis_asyncio.from_url(str(_settings.redis_url), encoding="utf-8", decode_responses=True)
        await FastAPILimiter.init(client, identifier=_identifier)
        logger.info("fastapi-limiter initialized (per-principal)")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Rate limiter disabled (Redis unavailable): %s", exc)


async def close_limiter() -> None:
    if _AVAILABLE:
        try:
            await FastAPILimiter.close()
        except Exception:  # noqa: BLE001
            pass


class OptionalRateLimiter:
    """A RateLimiter dependency that no-ops unless the limiter has been initialized."""

    def __init__(self, times: int, seconds: int):
        self._rl = RateLimiter(times=times, seconds=seconds) if _AVAILABLE else None

    async def __call__(self, request: Request, response: Response):
        if self._rl is None or getattr(FastAPILimiter, "redis", None) is None:
            return
        return await self._rl(request, response)


# Submit is the expensive path → tighter bucket.
submit_rate_limit = OptionalRateLimiter(times=30, seconds=60)
