"""Object storage abstraction — MinIO/S3 when configured, local filesystem otherwise.

``store_upload`` returns the value to persist as ``Document.storage_key``:
- MinIO mode → the object key (worker fetches via ``fget_object``);
- local mode → the absolute file path (worker reads it directly).
This mirrors ``worker.runtime.fetch_document``.
"""

from __future__ import annotations

import io
import uuid
from pathlib import Path

from api.settings import get_settings

_settings = get_settings()
_LOCAL_DIR = Path(__file__).resolve().parents[2] / "output" / "uploads"


def _minio_enabled() -> bool:
    return bool(_settings.s3_endpoint_url and _settings.s3_access_key)


def _logical_key(org_id: str, filename: str) -> str:
    """Build the storage key from the *basename* only (SEC-17).

    The filename is attacker-supplied. In local mode the key is joined onto `_LOCAL_DIR`, so a
    name like ``../../x.pdf`` would escape the upload root and the org prefix; in MinIO mode it
    would produce a key outside the tenant's namespace. `Path(...).name` drops every directory
    component after normalizing Windows separators — note it returns ``'..'`` for ``'../'`` rather than the
    empty string, so the traversal segments are rejected explicitly rather than assumed gone.
    """
    safe = Path((filename or "").replace("\\", "/")).name.strip()
    if safe in ("", ".", ".."):
        safe = "upload.bin"
    return f"{org_id}/{uuid.uuid4().hex}/{safe}"


def store_upload(org_id: str, filename: str, data: bytes, content_type: str | None) -> str:
    """Persist uploaded bytes; return the ``storage_key`` to save on the Document."""
    key = _logical_key(org_id, filename)
    if _minio_enabled():
        from minio import Minio  # noqa: PLC0415

        client = Minio(
            _settings.s3_endpoint_url.split("://", 1)[-1],
            access_key=_settings.s3_access_key,
            secret_key=_settings.s3_secret_key,
            secure=_settings.s3_secure,
        )
        if not client.bucket_exists(_settings.s3_bucket):
            client.make_bucket(_settings.s3_bucket)
        client.put_object(
            _settings.s3_bucket, key, io.BytesIO(data), length=len(data),
            content_type=content_type or "application/octet-stream",
        )
        return key
    # Local fallback (dev/tests): write under output/uploads and persist the path.
    path = _LOCAL_DIR / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)
