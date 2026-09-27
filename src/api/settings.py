"""Application settings — pydantic-settings (context7-validated patterns).

Sources, in priority order (highest first): init args → env vars (``FINALZE_*``) →
``.env`` → ``secrets_dir`` files (Docker/K8s) → defaults. Typed DSN fields validate
the Postgres/Redis/RabbitMQ URLs at startup.

Generate a real secret: ``openssl rand -hex 32`` → ``FINALZE_SECRET_KEY``.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import AmqpDsn, Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict

# Docker/K8s file-secrets mount. Only enable when present so local dev/tests don't warn;
# in containers /run/secrets exists and provides e.g. /run/secrets/finalze_secret_key.
_SECRETS_DIR = "/run/secrets" if os.path.isdir("/run/secrets") else None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FINALZE_",
        env_file=".env",
        # utf-8-sig, not utf-8: a Windows editor writes a BOM, and plain utf-8 keeps
        # it as part of the FIRST key — FINALZE_SECRET_KEY is then read as
        # "﻿finalze_secret_key", the field looks unset and the app refuses to
        # start. Compose strips the BOM itself, so `docker compose config` passes and
        # only the app fails. Identical behaviour on BOM-less files.
        env_file_encoding="utf-8-sig",
        secrets_dir=_SECRETS_DIR,  # Docker/K8s secrets when mounted; None on dev (see above)
        extra="ignore",
    )

    # --- Security ---------------------------------------------------------
    # REQUIRED, no default (SEC-8): this signs every access token. A default here
    # would mean any process started without FINALZE_SECRET_KEY signs tokens with a
    # value published in this repository — i.e. anyone can forge an admin JWT. Unset
    # is a startup failure on purpose. `openssl rand -hex 32`.
    secret_key: str = Field(
        min_length=16,
        description="Signing/token secret. REQUIRED — no default; set FINALZE_SECRET_KEY.",
    )
    access_token_lifetime_seconds: int = 1800  # short-lived (RedisStrategy revocable)

    # --- Datastores (typed DSNs) -----------------------------------------
    # Defaults are credential-FREE so nothing ships a working password. They point at
    # a local datastore for dev convenience only; compose always overrides them with
    # DSNs assembled from .env (see docker-compose.yml x-app-env).
    database_url: PostgresDsn = Field(
        default="postgresql+asyncpg://localhost:5432/finalze",
        description="Async SQLAlchemy DSN (asyncpg driver). Credentials come from the environment.",
    )
    redis_url: RedisDsn = Field(default="redis://localhost:6379/0")          # tokens, rate-limit, cache
    broker_url: AmqpDsn = Field(default="amqp://localhost:5672//")           # Celery broker (RabbitMQ)
    result_backend_url: RedisDsn = Field(default="redis://localhost:6379/1")    # Celery results

    # --- API ---------------------------------------------------------------
    allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost:8501"])
    max_upload_mb: int = 25
    # SEC-9: off by default — /docs and /redoc must be opted into (FINALZE_DOCS_ENABLED=true),
    # never inherited. The reverse proxy blocks the paths as well (traefik/dynamic.yml).
    docs_enabled: bool = False         # gate /docs & /redoc in production
    # MUST stay empty under the shipped deployment. Traefik routes by an ALLOWLIST of
    # absolute paths (/api/v1, /healthz, /readyz — traefik/dynamic.yml); a non-empty
    # root_path re-mounts every route under a prefix the allowlist does not cover, so
    # the app would be unreachable AND /metrics, /openapi.json would reappear under
    # that prefix. Changing it means rewriting the Traefik rule in the same commit.
    root_path: str = ""                # set when behind a proxy subpath (Traefik)

    # --- Object storage (MinIO / S3) --------------------------------------
    s3_endpoint_url: str | None = None
    s3_bucket: str = "finalze-documents"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_secure: bool = False

    # --- Pipeline defaults ------------------------------------------------
    default_accounting_standard: str = "NCT"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    """Cached singleton; import this rather than instantiating ``Settings`` directly."""
    return Settings()
