"""fastapi-users wiring — human auth with a revocable RedisStrategy + Argon2 hashing.

`RedisStrategy` stores opaque tokens in Redis with a lifetime, so **logout deletes the token →
instant server-side revocation** (context7-validated; cleaner than stateless JWT + a blocklist).
`BearerTransport` serves API/script/partner-user tokens; a `CookieTransport` can be added for the
browser SPA (BFF). Argon2 via pwdlib is the password hasher.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator

import redis.asyncio as redis_asyncio
from fastapi import Depends
from fastapi_users import BaseUserManager, FastAPIUsers, UUIDIDMixin
from fastapi_users.authentication import (
    AuthenticationBackend,
    BearerTransport,
    RedisStrategy,
)
from fastapi_users.db import SQLAlchemyUserDatabase
from fastapi_users.exceptions import InvalidPasswordException
from fastapi_users.password import PasswordHelper
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher
from sqlalchemy.ext.asyncio import AsyncSession

from api.db import get_async_session
from api.models import User
from api.settings import get_settings

logger = logging.getLogger(__name__)
_settings = get_settings()

# Argon2-only password hashing (context7-validated): PasswordHelper(PasswordHash((Argon2Hasher(),))).
password_helper = PasswordHelper(PasswordHash((Argon2Hasher(),)))

# Lazy async Redis client (no connection until first use).
redis_client = redis_asyncio.from_url(str(_settings.redis_url), decode_responses=True)


async def get_user_db(
    session: AsyncSession = Depends(get_async_session),
) -> AsyncGenerator[SQLAlchemyUserDatabase, None]:
    yield SQLAlchemyUserDatabase(session, User)


class UserManager(UUIDIDMixin, BaseUserManager[User, uuid.UUID]):
    reset_password_token_secret = _settings.secret_key
    verification_token_secret = _settings.secret_key

    async def on_after_register(self, user: User, request=None) -> None:
        logger.info("User registered: %s (org=%s)", user.id, user.org_id)

    async def validate_password(self, password: str, user) -> None:  # noqa: ANN001
        if len(password) < 10:
            raise InvalidPasswordException(reason="Password must be at least 10 characters.")
        if getattr(user, "email", None) and user.email.split("@")[0] in password:
            raise InvalidPasswordException(reason="Password must not contain the email name.")


async def get_user_manager(
    user_db: SQLAlchemyUserDatabase = Depends(get_user_db),
) -> AsyncGenerator[UserManager, None]:
    yield UserManager(user_db, password_helper)


def get_redis_strategy() -> RedisStrategy:
    return RedisStrategy(redis_client, lifetime_seconds=_settings.access_token_lifetime_seconds)


bearer_transport = BearerTransport(tokenUrl="api/v1/auth/jwt/login")

auth_backend = AuthenticationBackend(
    name="redis",
    transport=bearer_transport,
    get_strategy=get_redis_strategy,
)

fastapi_users = FastAPIUsers[User, uuid.UUID](get_user_manager, [auth_backend])

# Route guards.
current_active_user = fastapi_users.current_user(active=True)
current_superuser = fastapi_users.current_user(active=True, superuser=True)
# Optional variant for the unified principal (returns None instead of 401).
current_user_optional = fastapi_users.current_user(active=True, optional=True)
