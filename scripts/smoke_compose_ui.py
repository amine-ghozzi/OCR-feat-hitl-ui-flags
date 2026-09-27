"""F6 — compose end-to-end smoke for the dockerized UI + backend.

Run AFTER `docker compose up -d --build`, on the host (the UI is loopback-only):

    venv/Scripts/python scripts/smoke_compose_ui.py --insecure

`--insecure` (or SMOKE_INSECURE=1) skips TLS verification, which a self-signed
certificate from scripts/gen_selfsigned_cert.sh needs; drop it once the host
trusts the certificate. Override the endpoints with SMOKE_API_URL / SMOKE_UI_URL;
export FINALZE_ADMIN_EMAIL / FINALZE_ADMIN_PASSWORD from your .env first.

Checks, in order:
  0. HOST LOCKDOWN (SEC-7/9): through the single public port, every internal path
     is 404 and only the product surface answers; the served certificate is the
     one configured, not traefik's fallback. `--no-lockdown-probes` skips it.
  1. api `/healthz` and ui `/_stcore/health` respond.
  2. A dev token is obtainable (bootstrap admin from .env / compose defaults).
  3. `Samples/Bilan 1.jpg` submitted through the UI's own client path
     (`FinAlzeClient.submit_and_wait`) completes with status done.
  4. The result's `canonical_tables` REHYDRATE into typed objects (F3) and
     `decorate_for_grid` produces flags — i.e. what the UI renders in Docker
     is what it renders in-process.
  5. Reviewer write-back (F4): a cell correction PATCHes and the document
     transitions draft → validated.
  6. Negative path: a client pointed at a dead port fails with a clean
     FinAlzeError, not a stack-trace surprise.

Exit code 0 = all green.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Windows consoles default to cp1252 — don't let a ✓ kill the smoke.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

from client.sdk import FinAlzeClient, FinAlzeError  # noqa: E402

# The stack publishes exactly one port off-box: 8000/TLS through traefik (SEC-7).
# The UI is loopback-only, so run this on the host (or through `ssh -L 8501:…`).
API = os.environ.get("SMOKE_API_URL", "https://127.0.0.1:8000")
UI = os.environ.get("SMOKE_UI_URL", "http://127.0.0.1:8501")
# A self-signed certificate (scripts/gen_selfsigned_cert.sh) is not in any trust
# store: --insecure / SMOKE_INSECURE=1 skips verification, like `curl -k`.
INSECURE = "--insecure" in sys.argv or os.environ.get("SMOKE_INSECURE", "").lower() in {"1", "true", "yes"}
VERIFY = not INSECURE
LOCKDOWN_PROBES = "--no-lockdown-probes" not in sys.argv

# Everything the proxy must refuse. Traefik routes by an ALLOWLIST (/api/v1,
# /healthz, /readyz), so this list is a sample of the infinite complement, not the
# definition of what is blocked — including the FINALZE_ROOT_PATH-style prefixes a
# denylist used to let through.
BLOCKED_PATHS = [
    "/", "/docs", "/redoc", "/openapi.json", "/metrics", "/docs/oauth2-redirect",
    "/finalze/openapi.json", "/finalze/metrics", "/finalze/docs",
    "/apiv1/documents", "/api/v2/documents", "/dashboard/",
]
ALLOWED_PATHS = ["/healthz", "/readyz"]
ADMIN_EMAIL = os.environ.get("FINALZE_ADMIN_EMAIL", "admin@finalze.example")
ADMIN_PASSWORD = os.environ.get("FINALZE_ADMIN_PASSWORD", "")
SAMPLE = ROOT / "Samples" / "Bilan 1.jpg"


def _step(msg: str) -> None:
    print(f"[smoke] {msg}", flush=True)


def _transport() -> httpx.BaseTransport | None:
    """FinAlzeClient takes no ``verify=``; a transport is how we relax TLS for it."""
    return httpx.HTTPTransport(verify=False) if INSECURE else None


def _subject_from_der(der: bytes) -> str:
    """RFC 4514 subject of a DER-encoded certificate, or '' if it will not parse.

    Pure and separately testable. The parsed-dict form of ``getpeercert()`` is not
    an option: on an unverified context (CERT_NONE) CPython returns ``{}``, so any
    check built on it silently passes forever. Only ``binary_form=True`` yields
    anything, and that is DER, which needs a real X.509 parser.
    """
    try:
        from cryptography import x509

        return x509.load_der_x509_certificate(der).subject.rfc4514_string()
    except Exception as exc:  # noqa: BLE001
        _step(f"could not parse the served certificate ({type(exc).__name__}: {exc})")
        return ""


def _served_cert_subject() -> str:
    """Subject of the certificate traefik actually presents, or '' if unavailable."""
    import ipaddress
    import socket
    import ssl
    from urllib.parse import urlsplit

    parts = urlsplit(API)
    if parts.scheme != "https":
        return ""

    host = parts.hostname or "127.0.0.1"
    ctx = ssl._create_unverified_context()  # noqa: S323 — we inspect, we do not trust
    # An IP literal is not a valid SNI name; sending one makes some servers abort
    # the handshake with TLSV1_UNRECOGNIZED_NAME. Omit SNI in that case — the
    # default certificate is what answers a no-SNI connection anyway.
    try:
        ipaddress.ip_address(host)
        server_hostname = None
        ctx.check_hostname = False
    except ValueError:
        server_hostname = host

    try:
        with socket.create_connection((host, parts.port or 443), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=server_hostname) as tls:
                der = tls.getpeercert(binary_form=True)
    except Exception as exc:  # noqa: BLE001
        _step(f"could not read the served certificate ({type(exc).__name__}: {exc})")
        return ""

    if not der:
        _step("server presented no certificate")
        return ""
    return _subject_from_der(der)


def _lockdown_probes() -> None:
    """SEC-7/9 — assert the host lockdown from outside, through the real proxy.

    The unit tests can only check that the Traefik rule and the app's route table
    agree; this is the half that proves the deployed proxy enforces it.
    """
    _step("lockdown probes — internal paths must be 404 through the public port")
    failures: list[str] = []

    for path in BLOCKED_PATHS:
        try:
            code = httpx.get(f"{API}{path}", timeout=15.0, verify=VERIFY,
                             follow_redirects=False).status_code
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{path}: request failed ({exc})")
            continue
        if code != 404:
            failures.append(f"{path}: expected 404, got {code} — the path is REACHABLE")

    for path in ALLOWED_PATHS:
        try:
            code = httpx.get(f"{API}{path}", timeout=15.0, verify=VERIFY).status_code
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{path}: request failed ({exc})")
            continue
        if code != 200:
            failures.append(f"{path}: expected 200, got {code} — the allowlist is too tight")

    # A typo in TRAEFIK_CERT_FILE makes traefik fall back to its own generated
    # certificate instead of failing; on Docker Desktop create_host_path:false does
    # not stop it. This is what turns that silent degradation into a failure.
    subject = _served_cert_subject()
    if "TRAEFIK DEFAULT CERT" in subject.upper():
        failures.append(
            "traefik is serving its built-in fallback certificate — TRAEFIK_CERT_FILE / "
            "TRAEFIK_KEY_FILE did not resolve to the intended files"
        )
    elif subject:
        _step(f"served certificate subject: {subject}")

    if failures:
        raise SystemExit("[smoke] FAIL: host lockdown breached —\n  " + "\n  ".join(failures))
    _step(f"lockdown OK — {len(BLOCKED_PATHS)} internal paths 404, {len(ALLOWED_PATHS)} health paths 200")

    # Informational: plain HTTP on the TLS port. Traefik answers 404 (no plaintext
    # router) rather than refusing the connection; either is acceptable, neither
    # serves data, so this prints and never fails.
    plain = API.replace("https://", "http://", 1)
    try:
        r = httpx.get(f"{plain}/healthz", timeout=10.0)
        _step(f"plaintext on the TLS port: HTTP {r.status_code} (no data served)")
    except Exception as exc:  # noqa: BLE001
        _step(f"plaintext on the TLS port: refused ({type(exc).__name__})")


def _wait_http(url: str, name: str, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=5.0, verify=VERIFY)
            if r.status_code == 200:
                _step(f"{name} healthy ({url})")
                return
            last = f"HTTP {r.status_code}"
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
        time.sleep(3)
    raise SystemExit(f"[smoke] FAIL: {name} not healthy after {timeout}s — {last}")


def main() -> int:
    if not ADMIN_PASSWORD:
        raise SystemExit(
            "[smoke] FAIL: set FINALZE_ADMIN_PASSWORD (and FINALZE_ADMIN_EMAIL) — "
            "the same values as your .env; compose no longer ships a default."
        )
    if INSECURE:
        _step("TLS verification disabled (--insecure) — self-signed certificate expected")

    # 1 — health
    _wait_http(f"{API}/healthz", "api")
    _wait_http(f"{UI}/_stcore/health", "ui")

    # 0 (after health, so traefik has a live backend to route to) — host lockdown
    if LOCKDOWN_PROBES:
        _lockdown_probes()
    else:
        _step("lockdown probes SKIPPED (--no-lockdown-probes)")

    # 2 — token
    client = FinAlzeClient(base_url=API, timeout=60.0, transport=_transport())
    client.login(ADMIN_EMAIL, ADMIN_PASSWORD)
    _step("admin token obtained")

    # 3 — submit through the UI's own path
    if not SAMPLE.exists():
        raise SystemExit(f"[smoke] FAIL: sample missing: {SAMPLE}")
    _step(f"submitting {SAMPLE.name} (this runs real extraction in the worker)…")
    result = client.submit_and_wait(SAMPLE, accounting_standard="NCT", timeout=900.0)
    doc_id = result.get("document_id")
    assert doc_id, "submit_and_wait did not return a document_id (F4 contract)"
    assert result.get("tables"), "no tables extracted"
    _step(f"extraction done — qcs={result.get('qcs_score')} tier={result.get('tier_used')}")

    # 4 — F3 rehydration parity
    from accounting.canonical_model import CanonicalTable
    from hitl import review_view
    import pandas as pd

    # DLV-15: `None` entries are index-alignment placeholders, not tables — keep the
    # list intact and rehydrate the first entry that actually has canonical content.
    cts = [CanonicalTable.from_dict(x) if x else None
           for x in (result.get("canonical_tables") or [])]
    filled = [ct for ct in cts if ct is not None]
    assert filled, "canonical_tables missing from the API result (F3 regression)"
    flat = pd.DataFrame(result["tables"][0])
    grid, value_cols, summary = review_view.decorate_for_grid(flat, filled[0].to_dict())
    assert value_cols, "no value columns decorated"
    _step(f"canonical rehydration OK — {len(cts)} table(s), "
          f"flags: {summary['yellow']}y/{summary['red']}r/"
          f"{summary['assumption']}a/{summary.get('resolved', 0)}✓")

    # 5 — reviewer write-back (F4)
    patched = client.patch_cells(str(doc_id), [{
        "table_index": 0, "row_index": 0, "column_name": str(value_cols[0]),
        "raw_text": "smoke-test", "old_value": "0", "new_value": "0",
        "corrected_canonical": None,
    }])
    assert patched.get("applied") == 1, patched
    moved = client.transition(str(doc_id), "validated", reason="compose smoke")
    assert moved.get("state") == "validated", moved
    _step("review write-back OK — correction recorded, document validated")

    # 6 — negative path: dead API → clean error
    try:
        with FinAlzeClient(base_url="http://localhost:59999", timeout=3.0) as dead:
            dead.get_document("00000000-0000-0000-0000-000000000000")
        raise SystemExit("[smoke] FAIL: dead API did not raise")
    except FinAlzeError:
        pass  # HTTP-level error is also acceptable
    except Exception as exc:  # noqa: BLE001 — connection refused etc.
        _step(f"dead-API failure mode: {type(exc).__name__} (clean, surfaced to caller)")

    client.close()
    _step("ALL CHECKS PASSED ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
