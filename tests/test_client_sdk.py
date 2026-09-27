"""Phase 6 — client SDK unit tests (httpx.MockTransport; no live server)."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from client.sdk import FinAlzeClient, FinAlzeError  # noqa: E402


def _handler(job_status: str = "done"):
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path.endswith("/auth/jwt/login"):
            return httpx.Response(200, json={"access_token": "tok123", "token_type": "bearer"})
        if path.endswith("/documents") and method == "POST":
            assert request.headers.get("X-API-Key") == "fzabc.secret"
            return httpx.Response(202, json={"job_id": "j1", "document_id": "d1", "status": "queued"})
        if path.endswith("/documents/d1") and method == "GET":
            return httpx.Response(200, json={
                "id": "d1", "filename": "b.pdf", "accounting_standard": "NCT",
                "state": "draft", "created_at": "2026-01-01T00:00:00",
                "latest_job": {"id": "j1", "status": job_status, "tier_used": 1,
                               "qcs_score": 0.9, "hitl_required": False, "error": "boom"},
            })
        if path.endswith("/documents/d1/result"):
            return httpx.Response(200, json={"tier_used": 1, "qcs_score": 0.9, "tables": []})
        if path.endswith("/documents") and method == "GET":
            return httpx.Response(200, json={"items": [], "limit": 50, "offset": 0, "total": 0})
        return httpx.Response(404, json={"title": "Not Found", "detail": "nope", "status": 404})
    return handle


def test_login_sets_token():
    with FinAlzeClient(transport=httpx.MockTransport(_handler())) as c:
        tok = c.login("a@b.com", "pw")
        assert tok == "tok123"
        assert c._client.headers["Authorization"] == "Bearer tok123"


def test_submit_bytes_and_reads():
    with FinAlzeClient(api_key="fzabc.secret", transport=httpx.MockTransport(_handler())) as c:
        sub = c.submit_bytes("b.pdf", b"%PDF-1.4", accounting_standard="NCT")
        assert sub["document_id"] == "d1" and sub["status"] == "queued"
        assert c.get_document("d1")["latest_job"]["status"] == "done"
        assert c.get_result("d1")["qcs_score"] == 0.9
        assert c.list_documents()["total"] == 0


def test_submit_and_wait_returns_result():
    fd, tmp = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    Path(tmp).write_bytes(b"%PDF-1.4 fake")
    try:
        with FinAlzeClient(api_key="fzabc.secret", transport=httpx.MockTransport(_handler("done"))) as c:
            res = c.submit_and_wait(tmp, accounting_standard="NCT", poll_interval=0)
            assert res["qcs_score"] == 0.9
    finally:
        Path(tmp).unlink(missing_ok=True)


def test_failed_job_raises():
    fd, tmp = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    Path(tmp).write_bytes(b"%PDF-1.4 fake")
    try:
        with FinAlzeClient(api_key="fzabc.secret", transport=httpx.MockTransport(_handler("failed"))) as c:
            try:
                c.submit_and_wait(tmp, poll_interval=0)
                assert False, "expected FinAlzeError"
            except FinAlzeError as e:
                assert "failed" in str(e).lower()
    finally:
        Path(tmp).unlink(missing_ok=True)


def test_error_response_raises():
    with FinAlzeClient(api_key="fzabc.secret", transport=httpx.MockTransport(_handler())) as c:
        try:
            c.get_document("unknown")
            assert False, "expected FinAlzeError"
        except FinAlzeError as e:
            assert "404" in str(e)
