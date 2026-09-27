"""WP-2.12 — the production rebuild runbook is a script, so it gets a script's tests.

The runbook itself cannot be exercised from a test: it needs the server's ``.env``, its
TLS certificate and its data volumes, and ``scripts/verify_migrations.sh --yes`` starts
by deleting those volumes. What *can* be pinned without a Docker daemon is everything
that decides whether running it is safe at all:

  * it parses (``bash -n``);
  * ``--dry-run`` walks every step in the plan and prints every command it would run,
    while a stub ``docker`` on ``PATH`` records what was actually invoked — and nothing
    destructive is among it;
  * existing data volumes with no upgrade path chosen is a **refusal** (OPS-15), not a
    guess, and that refusal is not downgraded by ``--dry-run``;
  * without ``--yes`` the script cannot reach ``docker`` at all.

The ``docker`` stub is the whole point of the harness: the real CLI is on ``PATH`` on a
developer machine, and a bug that turned a printed command into an executed one would
otherwise be found by destroying someone's stack.

Also pinned here: the DOC-10 documents no longer tell anyone to run
``alembic upgrade head`` by hand, and the README section the script keeps pointing at
actually exists.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rebuild_prod.sh"
README = ROOT / "README.md"
QUICKSTART = ROOT / "context" / "E2E-Quickstart.md"
BACKEND_PDF_GEN = ROOT / "scripts" / "gen_backend_pdf.py"

# The plan the script prints, and the order it prints it in. Copied from the script on
# purpose: if a step is renamed, added or dropped, this list is the thing that says so.
EXPECTED_STEPS = [
    "preflight: repository ref and a clean working tree",
    "preflight: .env present and 'docker compose config' renders",
    "preflight: TRAEFIK_CERT_FILE / TRAEFIK_KEY_FILE resolve to files",
    "preflight: docker daemon reachable and enough free disk",
    "upgrade path: existing volumes and the OPS-15 credential choice",
    "build: docker compose build --pull",
    "start: docker compose up -d",
    "wait: the api container reports healthy",
    "schema: alembic current == head, plus the OPS-14 and NULL-org data checks",
    "verify: scripts/verify_migrations.sh --yes (only with --verify-migrations)",
    "smoke: scripts/smoke_compose_ui.py (lockdown + end-to-end + review write-back)",
    "post: docker compose ps, where the logs are, image cleanup",
]

# Every docker invocation the script is allowed to make while rehearsing. Read-only, all
# of them: a dry run that could not look at the host could not tell the operator which
# upgrade path they need.
READ_ONLY_DOCKER = ("info", "volume ls", "compose config", "compose ps -q", "inspect")

# Substrings that must never appear in what the stub was actually asked to do.
DESTRUCTIVE = ("down -v", "up -d", "build", "rm ", "prune", "exec", "restart", "stop")

STUB_DOCKER = """#!/usr/bin/env bash
# Test stub: records its arguments and answers the read-only queries the runbook makes.
printf '%s\\n' "$*" >> "$DOCKER_STUB_LOG"
case "$*" in
  info*)            echo "27.0.0-stub" ;;
  "volume ls -q")   printf '%s\\n' "${DOCKER_STUB_VOLUMES:-}" ;;
  *)                ;;
esac
exit 0
"""


def _find_bash() -> str:
    """A POSIX bash that can actually see this repository.

    On Windows ``shutil.which("bash")`` is usually ``C:\\Windows\\System32\\bash.exe``,
    the WSL launcher, whose filesystem does not contain ``C:/Users/...`` — it would fail
    every test for the wrong reason. So each candidate is probed by asking it to stat the
    script, and the first one that can is used. On Linux/macOS the first candidate
    answers immediately.
    """
    script = str(SCRIPT).replace("\\", "/")
    candidates = [
        os.environ.get("BASH_EXE"),
        shutil.which("bash"),
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        "/bin/bash",
        "/usr/bin/bash",
    ]
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            proc = subprocess.run(
                [candidate, "-c", f'test -f "{script}"'],
                capture_output=True, timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - env-dependent
            continue
        if proc.returncode == 0:
            return candidate
    pytest.skip("no bash that can read this repository is available")  # pragma: no cover


@pytest.fixture(scope="module")
def bash() -> str:
    return _find_bash()


@pytest.fixture
def harness(tmp_path: Path):
    """A stub ``docker`` on ``PATH``, a throwaway ``.env``, and a runner.

    ``ENV_FILE`` and ``REQUIRED_FREE_GB`` are honoured by the script, so the rehearsal is
    independent of whether this machine happens to have a real ``.env`` or free disk.
    """
    stub_dir = tmp_path / "stubbin"
    stub_dir.mkdir()
    stub = stub_dir / "docker"
    stub.write_text(STUB_DOCKER, encoding="utf-8", newline="\n")
    stub.chmod(0o755)

    log = tmp_path / "docker-invocations.log"
    cert = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    cert.write_text("-- not a certificate --\n", encoding="utf-8")
    key.write_text("-- not a key --\n", encoding="utf-8")

    env_file = tmp_path / "dotenv"
    env_file.write_text(
        "POSTGRES_USER=rebuild_user\n"
        "POSTGRES_DB=rebuild_db\n"
        f"TRAEFIK_CERT_FILE={str(cert).replace(chr(92), '/')}\n"
        f"TRAEFIK_KEY_FILE={str(key).replace(chr(92), '/')}\n"
        "FINALZE_ADMIN_EMAIL=admin@finalze.example\n"
        "FINALZE_ADMIN_PASSWORD=not-a-real-password\n",
        encoding="utf-8",
        newline="\n",
    )

    def run(*args: str, volumes: str = "", bash_exe: str | None = None):
        env = dict(os.environ)
        env["PATH"] = str(stub_dir) + os.pathsep + env.get("PATH", "")
        env["DOCKER_STUB_LOG"] = str(log)
        env["DOCKER_STUB_VOLUMES"] = volumes
        env["ENV_FILE"] = str(env_file).replace("\\", "/")
        env["REQUIRED_FREE_GB"] = "0"
        return subprocess.run(
            [bash_exe or _find_bash(), str(SCRIPT), *args],
            capture_output=True, text=True, timeout=300, env=env, cwd=str(ROOT),
        )

    def invocations() -> list[str]:
        if not log.exists():
            return []
        return [ln.strip() for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]

    return SimpleNamespace(run=run, invocations=invocations, env_file=env_file, log=log)


def test_script_parses(bash: str) -> None:
    """``bash -n`` on the committed file — a runbook that does not parse is worse than none."""
    proc = subprocess.run([bash, "-n", str(SCRIPT)], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.strip() == "", proc.stderr


def test_script_uses_a_bash_shebang_and_strict_mode() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    # Unset variables and a failed pipeline stage must stop the rebuild, not be skipped.
    assert "set -euo pipefail" in text


def test_dry_run_lists_every_step(harness, bash: str) -> None:
    """Every step is announced, every mutating command is printed, none of them runs."""
    proc = harness.run("--dry-run", bash_exe=bash)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout

    for i, label in enumerate(EXPECTED_STEPS, start=1):
        assert label in out, f"step {i} missing from the output: {label}"
        assert f"== step {i}/{len(EXPECTED_STEPS)} : {label}" in out, f"step {i} never executed"
    # The plan and the executed steps are printed from the same array, so each label
    # appears at least twice: once in the plan, once as a step header.
    assert out.count(EXPECTED_STEPS[0]) >= 2

    # The mutating commands are shown, prefixed, and demonstrably not executed.
    for command in ("docker compose build --pull", "docker compose up -d", "docker compose ps"):
        assert f"DRY-RUN  $ {command}" in out, command
    assert "python -m alembic current" in out
    assert "scripts/smoke_compose_ui.py --insecure" in out
    # The OPS-14 constraint probe is named in full, with the credentials from .env.
    assert "uq_variation_candidate_org_raw_matched" in out
    assert "-U rebuild_user -d rebuild_db" in out
    assert "nothing was built, started, stopped or deleted" in out

    calls = harness.invocations()
    assert calls, "the stub docker was never called — the harness is not wired up"
    for call in calls:
        assert call.startswith(READ_ONLY_DOCKER), f"dry run invoked docker with: {call!r}"
        for bad in DESTRUCTIVE:
            assert bad not in call, f"dry run invoked a destructive docker command: {call!r}"


def test_refuses_existing_volumes_without_choice(harness, bash: str) -> None:
    """OPS-15: the credentials inside an existing volume are not the ones in ``.env``.

    Guessing is either data loss or a stack that will not authenticate, so the script
    refuses — and ``--dry-run`` does not downgrade that refusal, because it is about the
    operator's decision rather than about the host.
    """
    volumes = "finalze_pgdata\nfinalze_rabbitmqdata\nfinalze_miniodata"
    proc = harness.run("--dry-run", volumes=volumes, bash_exe=bash)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    combined = proc.stdout + proc.stderr
    assert "no upgrade path chosen (OPS-15)" in combined
    assert "--rotated" in combined and "--reset-volumes" in combined
    assert "Upgrading an existing deployment" in combined
    # It stopped at the chooser: no build, no up.
    assert "== step 6/" not in proc.stdout
    for call in harness.invocations():
        for bad in DESTRUCTIVE:
            assert bad not in call, call


@pytest.mark.parametrize("flags", [(), ("--reset-volumes",), ("--rotated",), ("--verify-migrations",)])
def test_nothing_destructive_without_yes(harness, bash: str, flags: tuple[str, ...]) -> None:
    """A real run needs ``--yes``; without it the script never reaches ``docker`` at all."""
    proc = harness.run(*flags, volumes="finalze_pgdata", bash_exe=bash)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "without --yes" in proc.stderr
    assert harness.invocations() == [], harness.invocations()


def test_reset_volumes_is_printed_not_executed_in_a_dry_run(harness, bash: str) -> None:
    """The destructive branch still rehearses — it prints ``down -v`` and does not run it."""
    proc = harness.run("--dry-run", "--reset-volumes", volumes="finalze_pgdata", bash_exe=bash)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "*** DATA LOSS ***" in proc.stdout
    assert "DRY-RUN  $ docker compose down -v" in proc.stdout
    for call in harness.invocations():
        assert "down" not in call, call


def test_reset_volumes_and_rotated_are_mutually_exclusive(harness, bash: str) -> None:
    proc = harness.run("--dry-run", "--reset-volumes", "--rotated", bash_exe=bash)
    assert proc.returncode == 2
    assert "mutually exclusive" in proc.stderr
    assert harness.invocations() == []


def test_env_reader_tolerates_a_utf8_bom(harness, bash: str) -> None:
    """A Windows editor writes a BOM, and it sticks to the FIRST key of ``.env``.

    ``src/api/settings.py`` reads the file as ``utf-8-sig`` for exactly this reason and
    compose strips it too, so the script's own reader must not be the one place that
    trips over it — otherwise it would report ``POSTGRES_USER`` missing on a file the
    stack reads perfectly well.
    """
    text = harness.env_file.read_text(encoding="utf-8")
    harness.env_file.write_text(text, encoding="utf-8-sig", newline="\n")
    assert harness.env_file.read_bytes().startswith(b"\xef\xbb\xbf"), "fixture is not BOM-prefixed"

    proc = harness.run("--dry-run", bash_exe=bash)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # POSTGRES_USER is the first key in the fixture, i.e. the one the BOM would glue to.
    assert "-U rebuild_user -d rebuild_db" in proc.stdout


def test_readme_documents_the_runbook() -> None:
    """The script points every failure at the README; the section has to be there."""
    text = README.read_text(encoding="utf-8")
    assert "### Production rebuild (runbook)" in text
    for signature in (
        "invalid interpolation format",          # an empty ${VAR:?}
        "TLSV1_ALERT_INTERNAL_ERROR",            # the cert path is a directory
        "`alembic current` prints **nothing**",  # the migrate step never completed
        "FAIL: host lockdown breached",          # the smoke's lockdown probe
    ):
        assert signature in text, f"README is missing the failure signature: {signature}"
    assert "scripts/rebuild_prod.sh --dry-run" in text
    assert "OPS-14" in text and "OPS-15" in text


def test_doc10_startup_docs_use_api_migrate() -> None:
    """DOC-10 — neither document may still tell the operator to migrate by hand."""
    quickstart = QUICKSTART.read_text(encoding="utf-8")
    assert "alembic revision --autogenerate" not in quickstart
    assert "python -m api.migrate" in quickstart

    pdf_gen = BACKEND_PDF_GEN.read_text(encoding="utf-8")
    assert "No Alembic migration exists yet" not in pdf_gen
    assert "api.migrate" in pdf_gen
