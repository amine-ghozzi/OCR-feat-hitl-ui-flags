"""WP-1.2 — compose / host lockdown (SEC-7, SEC-8, SEC-9, SEC-10, GOV-2).

Pure text/YAML assertions over ``docker-compose.yml``, ``.env.example``,
``traefik/dynamic.yml`` and ``src/api/settings.py``. Nothing here needs Docker, a
network or a running stack: these are the invariants a reviewer would otherwise
have to re-grep by hand on every change to the deployment surface.

Compose is parsed with PyYAML rather than ``docker compose config`` on purpose —
the tests must run in CI without a daemon. PyYAML resolves the ``<<`` merge keys,
so per-service assertions see exactly what compose would render, and the
``${...}`` placeholders stay literal, which is what lands in review and in git.

The contract being frozen:
  * exactly one published port is reachable off-box — 8000, TLS, via traefik;
    every other published port is bound to 127.0.0.1;
  * no credential appears literally anywhere, and ``.env.example`` cannot boot a
    stack verbatim: every required variable is present but EMPTY, so
    `docker compose config` stops on the first one and names it;
  * no Docker socket mount, no traefik dashboard/insecure API, and no service
    quietly re-opening itself via ``network_mode``, ``expose``, ``privileged``…;
  * no ``Samples/`` mount (the corpus is client financial statements);
  * routing is an ALLOWLIST — every app route outside it is rewritten to a dead
    path, so ``/docs``, ``/metrics``, ``/openapi.json`` and anything added later
    are unreachable by construction rather than by enumeration;
  * TLS has a pinned floor and cipher policy.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"
DYNAMIC_PATH = REPO_ROOT / "traefik" / "dynamic.yml"
SETTINGS_PATH = REPO_ROOT / "src" / "api" / "settings.py"

COMPOSE_TEXT = COMPOSE_PATH.read_text(encoding="utf-8")
ENV_EXAMPLE_TEXT = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
DYNAMIC_TEXT = DYNAMIC_PATH.read_text(encoding="utf-8")
SETTINGS_TEXT = SETTINGS_PATH.read_text(encoding="utf-8")

# The one port that may face the network. Everything else is loopback-bound and
# reached over an SSH tunnel until per-user login lands (SEC-5).
PUBLIC_PORT = "8000"

# `${NAME:?}` / `${NAME:?message}` — a required variable with no default.
REQUIRED_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):\?[^}]*\}")

# `host:container` or `ip:host:container`, with optional /proto and quoting.
PORT_RE = re.compile(r"^(?:(?P<ip>[0-9a-fA-F:.\[\]]+):)?(?P<host>[0-9]+):(?P<container>[0-9]+)(?:/\w+)?$")

# The five paths that exist on the app (or would, with docs enabled) and must never
# be reachable through the proxy.
INTERNAL_PATHS = ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect", "/metrics"]

# Keys that re-open a service's network or privilege surface behind the reviewer's
# back. `expose` alone publishes nothing, but it is the documented precursor and has
# no legitimate use here; `env_file` would smuggle in credentials this file cannot see.
FORBIDDEN_SERVICE_KEYS = [
    "network_mode", "expose", "privileged", "cap_add", "pid", "userns_mode", "env_file",
]


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_TEXT)


def _dynamic() -> dict:
    return yaml.safe_load(DYNAMIC_TEXT)


def _services() -> dict:
    return _compose().get("services") or {}


def _env_example_assignments() -> dict[str, str]:
    """Only real, UNCOMMENTED ``KEY=value`` lines.

    A commented-out line is documentation, not configuration — counting it as
    "documented" is how a required variable goes missing from a deployer's .env.
    """
    out: dict[str, str] = {}
    for line in ENV_EXAMPLE_TEXT.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        out[key.strip()] = value.strip()
    return out


def _published_ports() -> list[tuple[str, str, str]]:
    """``(service, ip_or_empty, host_port)`` for every published port."""
    out: list[tuple[str, str, str]] = []
    for name, svc in _services().items():
        for entry in svc.get("ports") or []:
            if isinstance(entry, dict):  # compose long syntax
                out.append((name, str(entry.get("host_ip", "") or ""), str(entry.get("published", ""))))
                continue
            m = PORT_RE.match(str(entry).strip())
            assert m, f"{name}: unparsable ports entry {entry!r}"
            out.append((name, m.group("ip") or "", m.group("host")))
    return out


def _volume_sources(svc: dict) -> list[str]:
    """Host-side source of every mount, short or long syntax."""
    out: list[str] = []
    for entry in svc.get("volumes") or []:
        if isinstance(entry, dict):
            out.append(str(entry.get("source", "")))
        else:
            out.append(str(entry).split(":")[0])
    return out


def _allowlist_matchers() -> tuple[list[str], list[str]]:
    """``(prefixes, exact_paths)`` parsed out of the api router's rule."""
    rule = _dynamic()["http"]["routers"]["api"]["rule"]
    prefixes = re.findall(r"PathPrefix\(`([^`]+)`\)", rule)
    exact = re.findall(r"Path\(`([^`]+)`\)", rule)
    # `PathPrefix(...)` also matches the bare `Path(` regex; drop the overlap.
    exact = [p for p in exact if p not in prefixes]
    return prefixes, exact


def _allowlisted(path: str) -> bool:
    prefixes, exact = _allowlist_matchers()
    return path in exact or any(path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes)


def _app():
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from api.main import create_app

    return create_app()


# --------------------------------------------------------------------------- #
# SEC-8 / GOV-2 — no literal credentials anywhere
# --------------------------------------------------------------------------- #

def test_no_literal_credentials():
    """No usable secret in any tracked deployment file.

    The first four are the values the stack actually shipped with; the rest are the
    placeholder habits that replace them — a template that boots is a template
    nobody edits.
    """
    # Each needle is anchored with `(?![a-z])` so it matches a credential-shaped
    # token and not an identifier that happens to contain it — `replacePath` is a
    # Traefik middleware, `changeme` in `changemeter` would be noise.
    forbidden = [
        r"finalze:finalze", r"guest:guest", r"minioadmin(?![a-z])",
        r"changeme(?![a-z])", r"change-me(?![a-z])", r"replace(?![a-z])",
        r"password123",
    ]
    for path, text in (
        (COMPOSE_PATH, COMPOSE_TEXT),
        (ENV_EXAMPLE_PATH, ENV_EXAMPLE_TEXT),
        (DYNAMIC_PATH, DYNAMIC_TEXT),
        (SETTINGS_PATH, SETTINGS_TEXT),
    ):
        lowered = text.lower()
        for needle in forbidden:
            hit = re.search(needle, lowered)
            assert hit is None, \
                f"literal credential {hit.group(0)!r} in {path.name} at offset {hit.start()}"


def test_settings_secret_key_has_no_default():
    """A default signing secret is a forgeable admin token for every deployment."""
    assert re.search(r"secret_key:\s*str\s*=\s*Field\(\s*\n?\s*min_length=16", SETTINGS_TEXT), \
        "secret_key must be Field(min_length=16) with NO default"
    assert "default=" not in SETTINGS_TEXT.split("secret_key")[1].split(")")[0]


def test_env_file_with_bom_is_read(tmp_path, monkeypatch):
    """A .env saved by a Windows editor must still load (env_file_encoding utf-8-sig).

    With plain utf-8 the BOM stays glued to the first key, so FINALZE_SECRET_KEY is
    read as "\\ufefffinalze_secret_key", the required field looks unset and the app
    refuses to start. Compose strips the BOM itself, so `docker compose config`
    passes and only the app fails — a split failure that is hard to diagnose.

    The process environment wins over an env file, and conftest.py sets
    FINALZE_SECRET_KEY, so it has to be removed here or the test cannot discriminate.
    """
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from api.settings import Settings

    monkeypatch.delenv("FINALZE_SECRET_KEY", raising=False)

    secret = "bom-probe-secret-0123456789"
    env_file = tmp_path / ".env"
    env_file.write_text(f"FINALZE_SECRET_KEY={secret}\n", encoding="utf-8-sig")
    assert env_file.read_bytes().startswith(b"\xef\xbb\xbf"), "fixture is not BOM-prefixed"

    assert Settings(_env_file=str(env_file)).secret_key == secret


def test_settings_dsn_defaults_carry_no_credentials():
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from api.settings import Settings

    for field in ("database_url", "broker_url"):
        default = str(Settings.model_fields[field].default)
        assert "@" not in default, f"{field} default embeds credentials: {default}"


def test_every_credential_is_a_required_variable():
    """Credentials use `${VAR:?}`, never `${VAR:-default}`."""
    compose = _compose()
    app_env = compose["x-app-env"]
    for key in (
        "FINALZE_DATABASE_URL", "FINALZE_BROKER_URL",
        "FINALZE_S3_ACCESS_KEY", "FINALZE_S3_SECRET_KEY", "FINALZE_SECRET_KEY",
    ):
        value = str(app_env[key])
        assert REQUIRED_VAR_RE.search(value), f"{key} is not sourced from a ${{VAR:?}}"
        assert ":-" not in value, f"{key} still carries a default"

    services = compose["services"]
    for service, key in (
        ("postgres", "POSTGRES_USER"), ("postgres", "POSTGRES_PASSWORD"), ("postgres", "POSTGRES_DB"),
        ("rabbitmq", "RABBITMQ_DEFAULT_USER"), ("rabbitmq", "RABBITMQ_DEFAULT_PASS"),
        ("minio", "MINIO_ROOT_USER"), ("minio", "MINIO_ROOT_PASSWORD"),
        ("api", "FINALZE_ADMIN_EMAIL"), ("api", "FINALZE_ADMIN_PASSWORD"),
        ("ui", "FINALZE_ADMIN_EMAIL"), ("ui", "FINALZE_ADMIN_PASSWORD"),
    ):
        value = str(services[service]["environment"][key])
        assert REQUIRED_VAR_RE.fullmatch(value), f"{service}.{key} = {value!r} is not ${{VAR:?}}"


def test_flower_requires_basic_auth():
    """Flower shows task arguments (document ids, paths) — never unauthenticated."""
    assert "--basic_auth=${FLOWER_BASIC_AUTH:?}" in " ".join(_services()["flower"]["command"])


# --------------------------------------------------------------------------- #
# GOV-2 — .env.example is the deployment contract, and it must NOT boot
# --------------------------------------------------------------------------- #

def test_env_example_covers_all_required_vars():
    live = "\n".join(l for l in COMPOSE_TEXT.splitlines() if not l.lstrip().startswith("#"))
    required = set(REQUIRED_VAR_RE.findall(live))
    assert required, "no ${VAR:?} found in compose — the test would pass vacuously"

    documented = _env_example_assignments()
    missing = sorted(required - set(documented))
    assert not missing, f".env.example does not document: {missing}"


def test_env_example_required_values_are_empty():
    """A verbatim copy must FAIL, loudly, on the first required variable.

    `${VAR:?}` fires on empty as well as unset, so shipping every secret blank is
    what makes "I forgot to change the placeholder" impossible rather than likely.
    """
    live = "\n".join(l for l in COMPOSE_TEXT.splitlines() if not l.lstrip().startswith("#"))
    required = set(REQUIRED_VAR_RE.findall(live))
    assert required, "no ${VAR:?} found in compose — the test would pass vacuously"

    documented = _env_example_assignments()
    filled = {k: documented[k] for k in sorted(required) if documented.get(k)}
    assert not filled, f".env.example ships usable values for required vars: {filled}"


def test_env_example_documents_the_optional_settings():
    for name in [
        "FINALZE_OCR_ENGINE", "FINALZE_ALLOWED_ORIGINS", "FINALZE_MAX_UPLOAD_MB",
        "FINALZE_DOCS_ENABLED", "FINALZE_DEFAULT_ACCOUNTING_STANDARD",
        "FINALZE_S3_BUCKET", "FINALZE_S3_SECURE", "FINALZE_VARIATION_LOG",
        "FINALZE_API_KEY", "FINALZE_API_TOKEN",
    ]:
        assert name in _env_example_assignments(), f"{name} is not an active line in .env.example"


def test_env_example_has_no_inert_variables():
    """Every FINALZE_* line in the template must actually reach a container.

    A variable nobody reads looks like configuration and does nothing — the worst
    kind of documentation.
    """
    compose = _compose()
    reaching = set(compose["x-app-env"])
    for svc in compose["services"].values():
        env = svc.get("environment")
        if isinstance(env, dict):
            reaching |= set(env)

    declared = {k for k in _env_example_assignments() if k.startswith("FINALZE_")}
    inert = sorted(declared - reaching)
    assert not inert, f".env.example declares variables no container receives: {inert}"


def test_root_path_is_never_passed_to_a_container():
    """FINALZE_ROOT_PATH would re-mount every route under a prefix the Traefik
    allowlist does not cover — unreachable app, and /metrics back under the prefix."""
    compose = _compose()
    assert "FINALZE_ROOT_PATH" not in compose["x-app-env"]
    for name, svc in _services().items():
        env = svc.get("environment")
        if isinstance(env, dict):
            assert "FINALZE_ROOT_PATH" not in env, f"{name} receives FINALZE_ROOT_PATH"
    assert "FINALZE_ROOT_PATH" not in _env_example_assignments()
    assert "root_path" in SETTINGS_TEXT and "allowlist" in SETTINGS_TEXT.lower(), \
        "settings.py must say why root_path has to stay empty"


# --------------------------------------------------------------------------- #
# SEC-7 / SEC-9 — one public port, nothing re-opening itself
# --------------------------------------------------------------------------- #

def test_all_published_ports_loopback_except_8000():
    published = _published_ports()
    assert published, "no ports parsed — the test would pass vacuously"

    off_box = [(s, i, h) for s, i, h in published if i not in ("127.0.0.1", "::1")]
    assert [h for _, _, h in off_box] == [PUBLIC_PORT], \
        f"expected only {PUBLIC_PORT} to face the network, got {off_box}"
    assert [s for s, _, _ in off_box] == ["traefik"], \
        f"{PUBLIC_PORT} must be published by traefik, got {off_box}"

    for svc, ip, host in published:
        if host != PUBLIC_PORT:
            assert ip == "127.0.0.1", f"{svc} publishes {host} on {ip or '0.0.0.0'}"


def test_api_and_worker_publish_nothing():
    services = _services()
    for name in ("api", "worker", "worker-gpu"):
        assert not services[name].get("ports"), f"{name} must not publish a port"


def test_no_service_reopens_its_network_or_privileges():
    """`network_mode: host` bypasses every port binding above without touching one."""
    for name, svc in _services().items():
        present = [k for k in FORBIDDEN_SERVICE_KEYS if k in svc]
        assert not present, f"service {name} declares {present}"


def test_traefik_serves_tls_on_the_public_port():
    traefik = _services()["traefik"]
    command = " ".join(traefik["command"])
    assert f"--entrypoints.websecure.address=:{PUBLIC_PORT}" in command
    assert "--entrypoints.web.address" not in command, "plaintext entrypoint reintroduced"

    sources = _volume_sources(traefik)
    assert "${TRAEFIK_CERT_FILE:?}" in sources and "${TRAEFIK_KEY_FILE:?}" in sources

    for entry in traefik["volumes"]:
        assert isinstance(entry, dict), f"traefik mount must use long syntax: {entry}"
        assert entry.get("read_only") is True, f"traefik mount not read-only: {entry}"
        assert entry.get("bind", {}).get("create_host_path") is False, \
            f"a typo in {entry.get('source')} must fail, not create an empty dir: {entry}"


def test_traefik_ping_is_not_published():
    """The healthcheck's ping endpoint lives on the internal entrypoint only."""
    traefik = _services()["traefik"]
    assert "--ping=true" in " ".join(traefik["command"])
    assert "--ping" in " ".join(traefik["healthcheck"]["test"])
    assert [str(p) for p in traefik["ports"]] == [f"{PUBLIC_PORT}:{PUBLIC_PORT}"]


def test_flower_does_not_inherit_the_api_healthcheck():
    """Dockerfile.api probes :8000/healthz, which flower never serves."""
    assert _services()["flower"]["healthcheck"] == {"disable": True}


def test_images_are_pinned():
    """`minio/minio` with no tag is a moving :latest under the object store."""
    for name, svc in _services().items():
        image = svc.get("image")
        if not image:
            continue
        assert ":" in image, f"{name}: image {image!r} has no tag (floating :latest)"
        assert not image.endswith(":latest"), f"{name}: image {image!r} is :latest"


# --------------------------------------------------------------------------- #
# SEC-8 — no Docker socket, no traefik dashboard / insecure API
# --------------------------------------------------------------------------- #

def test_no_docker_socket_or_insecure_api():
    lowered = COMPOSE_TEXT.lower()
    for needle in ("docker.sock", "api.insecure", "api.dashboard", "providers.docker"):
        assert needle not in lowered, f"{needle} is still in docker-compose.yml"

    command = " ".join(_services()["traefik"]["command"])
    assert "--providers.file.filename=/etc/traefik/dynamic.yml" in command
    assert "--providers.file.watch=true" in command


def test_no_traefik_labels_on_any_service():
    """Label-driven routing is how a compromised container publishes itself."""
    for name, svc in _services().items():
        labels = svc.get("labels") or []
        flat = labels if isinstance(labels, list) else list(labels.keys())
        assert not [x for x in flat if "traefik" in str(x).lower()], f"{name} carries traefik labels"


# --------------------------------------------------------------------------- #
# SEC-10 — no Samples mount
# --------------------------------------------------------------------------- #

def test_no_samples_mount():
    """The corpus is client financial statements — it never rides into a container."""
    for name, svc in _services().items():
        for source in _volume_sources(svc):
            assert not source.rstrip("/").lower().endswith("samples"), \
                f"{name} mounts the corpus: {source}"


# --------------------------------------------------------------------------- #
# SEC-9 — routing is an allowlist
# --------------------------------------------------------------------------- #

def test_traefik_blocks_internal_paths():
    """Every internal path falls to the catch-all, which rewrites it to a dead route."""
    routers = _dynamic()["http"]["routers"]
    api, blocker = routers["api"], routers["block-internal"]

    assert blocker["rule"] == "PathPrefix(`/`)", "the catch-all must match everything"
    assert api["priority"] > blocker["priority"], "the allowlist must win over the catch-all"
    assert api["entryPoints"] == blocker["entryPoints"] == ["websecure"]
    assert blocker["middlewares"] == ["block-internal"]

    for path in INTERNAL_PATHS:
        assert not _allowlisted(path), f"{path} is inside the allowlist"

    # replacePath is the mechanism: the file provider has no null backend (an empty
    # loadBalancer yields 502, not 404) and `errors` can only substitute a body for a
    # status the backend already returned.
    replaced = _dynamic()["http"]["middlewares"]["block-internal"]["replacePath"]["path"]
    assert replaced.startswith("/") and replaced not in {"/", *INTERNAL_PATHS}


def test_allowlist_covers_every_real_route_and_no_internal_one():
    """The static half of the end-to-end proof (the other half is the smoke script).

    Ties the Traefik rule to the app's actual route table, so adding a router
    outside /api/v1 fails here instead of silently 404-ing in production — and so a
    future internal endpoint cannot quietly land inside the allowlist.
    """
    routes = {getattr(r, "path", None) for r in _app().routes} - {None}
    assert routes, "no routes found — the test would pass vacuously"

    unreachable = sorted(p for p in routes if p not in INTERNAL_PATHS and not _allowlisted(p))
    assert not unreachable, f"real routes the proxy would block: {unreachable}"

    exposed = sorted(p for p in INTERNAL_PATHS if _allowlisted(p))
    assert not exposed, f"internal paths the proxy would serve: {exposed}"


def test_replaced_path_is_a_404_on_the_real_app():
    """``block-internal`` only works because the app never registered its target."""
    import asyncio

    from httpx import ASGITransport, AsyncClient

    replaced = _dynamic()["http"]["middlewares"]["block-internal"]["replacePath"]["path"]
    app = _app()

    async def _go():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            return await c.get(replaced)

    assert asyncio.run(_go()).status_code == 404, f"{replaced} is a live route — nothing is blocked"


def test_documents_why_the_proxy_rule_is_load_bearing():
    """/metrics and /openapi.json are mounted unconditionally.

    /docs and /redoc are already gone (docs_enabled=False), but these two are not —
    without the Traefik catch-all they would be served to anyone reaching port 8000.
    This is the reason the allowlist exists, pinned so it cannot be argued away.
    """
    routes = {getattr(r, "path", None) for r in _app().routes}
    assert "/metrics" in routes
    assert "/openapi.json" in routes


def test_traefik_routes_only_to_the_api_service():
    dynamic = _dynamic()
    services = dynamic["http"]["services"]
    assert set(services) == {"api"}
    assert [s["url"] for s in services["api"]["loadBalancer"]["servers"]] == ["http://api:8000"]

    # The backend must be a real compose service, reachable by that name on the
    # compose network — a typo here is a 502 nobody sees until deployment.
    assert "api" in _services(), "traefik routes to a service compose does not define"

    for name, router in dynamic["http"]["routers"].items():
        assert router["service"] == "api", f"router {name} points elsewhere"
        assert "tls" in router, f"router {name} would answer in plaintext"


# --------------------------------------------------------------------------- #
# TLS policy + certificate handling
# --------------------------------------------------------------------------- #

def test_tls_policy_is_pinned():
    options = _dynamic()["tls"]["options"]["default"]
    assert options["minVersion"] == "VersionTLS12"

    suites = options["cipherSuites"]
    assert suites, "cipherSuites must be explicit, not whatever the build defaults to"
    for suite in suites:
        assert suite.startswith("TLS_ECDHE"), f"{suite} is not forward-secret"
        assert "GCM" in suite or "CHACHA20" in suite, f"{suite} is not AEAD"
    # sniStrict would break every IP-literal client (the tunnel, the smoke script).
    assert "sniStrict" not in options


def test_traefik_tls_points_at_the_mounted_certificate():
    dynamic = _dynamic()
    cert = dynamic["tls"]["certificates"][0]
    assert cert["certFile"] == "/etc/traefik/certs/server.crt"
    assert cert["keyFile"] == "/etc/traefik/certs/server.key"
    # Answers requests with no matching SNI (curl https://127.0.0.1:8000/).
    assert dynamic["tls"]["stores"]["default"]["defaultCertificate"] == cert

    targets = [v.get("target") for v in _services()["traefik"]["volumes"] if isinstance(v, dict)]
    assert cert["certFile"] in targets and cert["keyFile"] in targets


def test_smoke_cert_probe_reads_the_subject_from_der():
    """The smoke script's fallback-certificate detector must actually see a subject.

    It used to call ``getpeercert()`` on an unverified context, which returns ``{}``
    — so the check reported '' for every certificate and could never fire, including
    against a traefik genuinely serving its own "TRAEFIK DEFAULT CERT". The parser
    is factored out precisely so this can be asserted without a network.
    """
    import datetime
    import importlib.util

    x509 = pytest.importorskip("cryptography.x509")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    spec = importlib.util.spec_from_file_location(
        "_smoke_probe", REPO_ROOT / "scripts" / "smoke_compose_ui.py"
    )
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TRAEFIK DEFAULT CERT")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    der = cert.public_bytes(serialization.Encoding.DER)

    subject = smoke._subject_from_der(der)
    assert "TRAEFIK DEFAULT CERT" in subject.upper(), subject
    # Garbage must degrade to '' rather than raise inside the smoke run.
    assert smoke._subject_from_der(b"not-a-certificate") == ""


def test_cert_dir_is_gitignored():
    """A self-signed key is still a key."""
    ignored = [l.strip() for l in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
    assert "certs/" in ignored


# --------------------------------------------------------------------------- #
# WP-1.0 regression guards
# --------------------------------------------------------------------------- #

def test_variation_log_and_data_volume_survive():
    """WP-1.0's learning-log redirect (GOV-4) must not be lost to this rewrite."""
    compose = _compose()
    assert "{org_id}" in compose["x-app-env"]["FINALZE_VARIATION_LOG"]
    assert "finalze_data" in compose["volumes"]
    for name in ("api", "worker"):
        assert any("finalze_data:/app/data" in v for v in compose["services"][name]["volumes"])


def test_allowed_origins_is_still_a_json_list_string():
    """pydantic parses list[str] from JSON — a bare comma list raises at startup."""
    value = _compose()["x-app-env"]["FINALZE_ALLOWED_ORIGINS"]
    # Strip the `${VAR:-default}` wrapper compose applies around the JSON default.
    default = re.fullmatch(r"\$\{FINALZE_ALLOWED_ORIGINS:-(.*)\}", value)
    parsed = json.loads(default.group(1) if default else value)
    assert isinstance(parsed, list) and all(isinstance(x, str) for x in parsed)


def test_docs_disabled_by_default_in_settings():
    """SEC-9 second line of defence: the app does not mount /docs unless asked."""
    assert re.search(r"^\s*docs_enabled:\s*bool\s*=\s*False", SETTINGS_TEXT, re.MULTILINE)
