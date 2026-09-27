"""Repo hygiene — files a pipeline run writes must never be tracked (GOV-4).

The variation learning log records which banks a client borrows from. It is written
on every extraction run, so if it were tracked, any run would dirty the repo and the
log itself would be published with it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _git(*args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(ROOT), capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"git unavailable: {exc}")
    if proc.returncode != 0:  # pragma: no cover - env-dependent
        pytest.skip(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def test_variation_log_untracked():
    """`git ls-files data/variation_candidates.jsonl` must come back empty."""
    assert _git("ls-files", "data/variation_candidates.jsonl").strip() == ""
    # The per-tenant directory holds only its .gitkeep — no tenant log is tracked.
    tracked = [ln for ln in _git("ls-files", "data/variation_candidates/").splitlines() if ln.strip()]
    assert tracked == ["data/variation_candidates/.gitkeep"], tracked
