"""OCR-5 — `tools/skew_report.py` measures page skew. It must never correct it.

The register row says "no deskew"; decision Q4 answers it with "measure first". This test
pins the estimator against pages skewed by a KNOWN angle, so the number the report hands
the decision is a number somebody checked. Tolerance ±0.3 degrees — the decision threshold
is 1 degree, so the estimator has to be good to well under that to be worth anything.

The fixture is synthetic: bars of "text" drawn with PIL, rotated by a known angle.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.skew_report import measure_skew  # noqa: E402

TOLERANCE = 0.3


def _text_page(width: int = 700, height: int = 900) -> Image.Image:
    """A white page with black bars where a scan would have lines of text."""
    page = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(page)
    for i, top in enumerate(range(90, height - 90, 36)):
        # Ragged right edge, like real lines — a perfect rectangle block is an
        # easier target than a page of text.
        right = width - 90 - (i % 4) * 40
        draw.rectangle([90, top, right, top + 9], fill=0)
    return page


def _skewed(page: Image.Image, degrees: float) -> Image.Image:
    """The page as a scanner would have picked it up off by `degrees`.

    PIL rotates counter-clockwise, so rotating by ``-degrees`` produces a page
    that needs ``+degrees`` to come level — exactly what `measure_skew` reports.
    """
    return page.rotate(-degrees, resample=Image.Resampling.BILINEAR,
                       expand=False, fillcolor=255)


def test_synthetic_rotated_page_reports_angle():
    page = _text_page()
    for expected in (0.0, 1.5, -2.5, 0.6):
        measured, ink = measure_skew(_skewed(page, expected))
        assert measured is not None, f"no angle measured for {expected}"
        assert abs(measured - expected) <= TOLERANCE, (
            f"expected {expected} deg, measured {measured} deg")
        assert 0.0 < ink < 1.0


def test_blank_page_reports_no_angle():
    """A blank render has no profile — report `null`, never a fabricated angle."""
    measured, ink = measure_skew(Image.new("L", (400, 500), 255))

    assert measured is None
    assert ink == 0.0
