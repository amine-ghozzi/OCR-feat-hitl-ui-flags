"""Thin synchronous FinAlze API client (httpx).

Auth via a bearer token (human) or `X-API-Key` (partner). `submit_and_wait` polls the job to
completion. A custom `transport` can be injected for tests (httpx.MockTransport).
"""

from __future__ import annotations

import mimetypes
import time
from pathlib import Path
from typing import Any

import httpx


class FinAlzeError(Exception):
    """Raised on a non-2xx API response (carries status + problem detail)."""


def _guess_content_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _raise_for_status(r: httpx.Response) -> None:
    if r.status_code >= 400:
        try:
            problem = r.json()
            detail = problem.get("detail") or problem.get("title") or problem
        except Exception:  # noqa: BLE001
            detail = r.text
        raise FinAlzeError(f"{r.status_code}: {detail}")


class FinAlzeClient:
    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        *,
        token: str | None = None,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        prefix: str = "/api/v1",
    ):
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if api_key:
            headers["X-API-Key"] = api_key
        self.prefix = prefix
        self._client = httpx.Client(
            base_url=base_url, headers=headers, transport=transport, timeout=timeout
        )

    # -- lifecycle --
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "FinAlzeClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- auth --
    def login(self, email: str, password: str) -> str:
        r = self._client.post(f"{self.prefix}/auth/jwt/login",
                              data={"username": email, "password": password})
        _raise_for_status(r)
        token = r.json()["access_token"]
        self._client.headers["Authorization"] = f"Bearer {token}"
        return token

    # -- documents --
    def submit_bytes(
        self, filename: str, data: bytes, *,
        accounting_standard: str | None = None, content_type: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        files = {"file": (filename, data, content_type or _guess_content_type(filename))}
        form = {"accounting_standard": accounting_standard} if accounting_standard else {}
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        r = self._client.post(f"{self.prefix}/documents", files=files, data=form, headers=headers)
        _raise_for_status(r)
        return r.json()

    def submit(self, file_path: str | Path, **kwargs) -> dict[str, Any]:
        p = Path(file_path)
        return self.submit_bytes(p.name, p.read_bytes(), **kwargs)

    def get_document(self, document_id: str) -> dict[str, Any]:
        r = self._client.get(f"{self.prefix}/documents/{document_id}")
        _raise_for_status(r)
        return r.json()

    def get_result(self, document_id: str) -> dict[str, Any]:
        r = self._client.get(f"{self.prefix}/documents/{document_id}/result")
        _raise_for_status(r)
        return r.json()

    def list_documents(self, **params) -> dict[str, Any]:
        r = self._client.get(f"{self.prefix}/documents", params=params)
        _raise_for_status(r)
        return r.json()

    def submit_and_wait(
        self, file_path: str | Path, *, accounting_standard: str | None = None,
        poll_interval: float = 2.0, timeout: float = 300.0,
    ) -> dict[str, Any]:
        sub = self.submit(file_path, accounting_standard=accounting_standard)
        document_id = sub["document_id"]
        deadline = time.monotonic() + timeout
        while time.monotonic() <= deadline:
            doc = self.get_document(document_id)
            job = doc.get("latest_job") or {}
            if job.get("status") == "done":
                result = self.get_result(document_id)
                # F4: callers (the HITL UI) need the id to write reviews back.
                if isinstance(result, dict):
                    result.setdefault("document_id", document_id)
                return result
            if job.get("status") == "failed":
                raise FinAlzeError(f"Extraction failed: {job.get('error')}")
            time.sleep(poll_interval)
        raise FinAlzeError("Timed out waiting for extraction result")

    # -- review (HITL write-back, F4) --
    def patch_cells(
        self, document_id: str, corrections: list[dict[str, Any]], *,
        if_match: int | None = None,
    ) -> dict[str, Any]:
        """Record reviewer cell corrections (``PATCH /documents/{id}/cells``).

        Each correction: ``{table_index, row_index, column_name, raw_text,
        old_value, new_value, corrected_canonical}`` — appended as Correction
        rows; a supplied canonical mapping also seeds the feedback loop.
        """
        headers = {"If-Match": str(if_match)} if if_match is not None else {}
        r = self._client.patch(
            f"{self.prefix}/documents/{document_id}/cells",
            json={"corrections": corrections}, headers=headers,
        )
        _raise_for_status(r)
        return r.json()

    def transition(
        self, document_id: str, to_state: str, *,
        reason: str | None = None, if_match: int | None = None,
    ) -> dict[str, Any]:
        """Move a document through the review state machine
        (``draft → validated|rejected``, ``validated → archived``)."""
        headers = {"If-Match": str(if_match)} if if_match is not None else {}
        r = self._client.post(
            f"{self.prefix}/documents/{document_id}/transition",
            json={"to_state": to_state, "reason": reason}, headers=headers,
        )
        _raise_for_status(r)
        return r.json()
