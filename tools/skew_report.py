#!/usr/bin/env python
"""Skew report — MEASURE page skew on the scanned packs (OCR-5). Never corrects it.

    python tools/skew_report.py --out <scratch>/skew.json "Samples/Sample - OF/x.pdf" ...

Register row OCR-5 says "no deskew"; decision Q4 (form, 2026-09-15) answers it with
*measure first, correct only if it matters*. A deskew is a resampling of the page that
moves every OCR box and every `table_boxes` coordinate, so it is not something to add on
the strength of a document that says scanned pages are usually skewed. This tool produces
the number that decides it: if no page in the corpus is off by more than ~1 degree, the
deskew (backlog B-2) is not worth its regression surface.

**This tool measures. It does not rotate anything, and nothing in `src/` imports it.**

Method — projection profile, the standard document-skew estimator. Render the page to a
bitmap, binarise it to ink/no-ink, then rotate through a range of candidate angles; at
each angle sum the ink per pixel row. When the text lines are level the row sums alternate
hard between "line" and "gutter", so the sum of squared differences between adjacent rows
peaks. A coarse sweep finds the bucket, a fine sweep finds the angle. numpy + Pillow only:
OpenCV is importable here (4.13.0, via rapidocr) but adds nothing this needs, and keeping
it out keeps the test runnable anywhere.

Sign convention: `degrees` is the rotation that would LEVEL the page — positive means
counter-clockwise, i.e. `image.rotate(degrees)` deskews it. A page with too little ink to
measure reports `null`.

Output discipline (repo CLAUDE.md): five documents under `Samples/` are non-public. This
tool only ever emits a file name, a page number, a page size and an angle — counts and
structural facts, never a value, a label or a line of text. It refuses to write its report
inside the repository: the report goes to a scratch path.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageOps

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_DPI = 150
# Beyond ~5 degrees a page is not "skewed", it is rotated, and /Rotate (OCR-1/2/3) or the
# EXIF tag (OCR-4) is the thing that explains it.
DEFAULT_MAX_ANGLE = 5.0
COARSE_STEP = 0.5
FINE_STEP = 0.05
# Longest side the estimator works on. The angle does not need the full raster and the
# search is ~40 rotations per page.
WORK_MAX_SIDE = 1000
# A page this empty (share of ink pixels) has no profile to measure.
MIN_INK_FRACTION = 0.001


# --- estimator -------------------------------------------------------------
def _otsu_threshold(values: np.ndarray, bins: int = 256) -> float:
    """Otsu's threshold for a [0, 1] array — splits ink from paper."""
    hist, edges = np.histogram(values, bins=bins, range=(0.0, 1.0))
    hist = hist.astype(np.float64)
    total = hist.sum()
    if total <= 0:
        return 0.5
    centers = (edges[:-1] + edges[1:]) / 2.0
    weight_bg = np.cumsum(hist)
    weight_fg = total - weight_bg
    sum_all = float((hist * centers).sum())
    sum_bg = np.cumsum(hist * centers)
    valid = (weight_bg > 0) & (weight_fg > 0)
    if not valid.any():
        return 0.5
    mean_bg = np.divide(sum_bg, weight_bg, out=np.zeros_like(sum_bg), where=weight_bg > 0)
    mean_fg = np.divide(sum_all - sum_bg, weight_fg,
                        out=np.zeros_like(sum_bg), where=weight_fg > 0)
    between = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
    between[~valid] = -1.0
    return float(centers[int(np.argmax(between))])


def ink_mask(image: Image.Image, max_side: int = WORK_MAX_SIDE) -> Optional[Image.Image]:
    """Binarise *image* to an 'L' mask where 255 = ink, downscaled for the search.

    Returns None when the page is uniform (a blank render has no threshold).
    """
    gray = image.convert("L")
    longest = max(gray.size)
    if longest > max_side:
        scale = max_side / float(longest)
        gray = gray.resize(
            (max(1, int(gray.width * scale)), max(1, int(gray.height * scale))),
            Image.Resampling.BILINEAR,
        )
    arr = np.asarray(gray, dtype=np.float32)
    lo, hi = float(arr.min()), float(arr.max())
    if hi - lo < 1e-6:
        return None
    norm = (arr - lo) / (hi - lo)
    ink = (norm < _otsu_threshold(norm)).astype(np.uint8) * 255
    return Image.fromarray(ink)   # a 2-D uint8 array is mode "L"


def _profile_score(mask: Image.Image, angle: float) -> float:
    """Sharpness of the horizontal ink profile after rotating *mask* by *angle*.

    `fillcolor=0` so the corners the rotation exposes contribute no ink.
    """
    if angle:
        mask = mask.rotate(angle, resample=Image.Resampling.BILINEAR,
                           expand=False, fillcolor=0)
    rows = np.asarray(mask, dtype=np.float32).sum(axis=1)
    if rows.size < 2:
        return 0.0
    return float(np.square(np.diff(rows)).sum())


def measure_skew(
    image: Image.Image,
    max_angle: float = DEFAULT_MAX_ANGLE,
    coarse_step: float = COARSE_STEP,
    fine_step: float = FINE_STEP,
) -> Tuple[Optional[float], float]:
    """Measure the skew of *image* by projection profile.

    Returns ``(degrees, ink_fraction)``. ``degrees`` is the counter-clockwise
    rotation that would level the page, or None when the page carries too little
    ink to measure. **Nothing is rotated except the throw-away search copies.**
    """
    mask = ink_mask(image)
    if mask is None:
        return None, 0.0
    arr = np.asarray(mask, dtype=np.uint8)
    ink_fraction = float((arr > 0).mean())
    if ink_fraction < MIN_INK_FRACTION:
        return None, round(ink_fraction, 4)

    def best_of(candidates) -> float:
        return max(candidates, key=lambda a: _profile_score(mask, a))

    coarse = np.arange(-max_angle, max_angle + coarse_step / 2, coarse_step)
    best = best_of(float(a) for a in coarse)
    fine = np.arange(best - coarse_step, best + coarse_step + fine_step / 2, fine_step)
    best = round(float(best_of(a for a in fine if abs(a) <= max_angle + 1e-9)), 2)
    # `np.arange` lands on -0.0 for a level page; report a plain 0.0.
    return (best or 0.0), round(ink_fraction, 4)


# --- rendering -------------------------------------------------------------
def render_pages(path: Path, dpi: int = DEFAULT_DPI) -> Iterator[Tuple[int, Image.Image]]:
    """Yield ``(page_number, image)`` for a PDF or a single image file.

    Images go through `exif_transpose` first, so the skew measured is the skew of
    what OCR now actually sees (OCR-4).
    """
    if path.suffix.lower() == ".pdf":
        import fitz  # PyMuPDF

        doc = fitz.open(str(path))
        try:
            for index in range(len(doc)):
                pix = doc[index].get_pixmap(dpi=dpi)
                mode = "RGBA" if pix.alpha else "RGB"
                yield index + 1, Image.frombytes(mode, (pix.width, pix.height), pix.samples)
        finally:
            doc.close()
        return

    with Image.open(path) as image:
        upright = ImageOps.exif_transpose(image) or image
        yield 1, upright.copy()


def measure_document(path: Path, dpi: int = DEFAULT_DPI,
                     max_angle: float = DEFAULT_MAX_ANGLE) -> List[dict]:
    """One row per page: ``{doc_id, page, degrees, ink_fraction, size}``."""
    rows: List[dict] = []
    for page_no, image in render_pages(path, dpi=dpi):
        degrees, ink = measure_skew(image, max_angle=max_angle)
        rows.append({
            "doc_id": path.name,
            "page": page_no,
            "degrees": degrees,
            "ink_fraction": ink,
            "size": list(image.size),
        })
    return rows


# --- CLI -------------------------------------------------------------------
def _is_inside_repo(path: Path) -> bool:
    try:
        path.resolve().relative_to(REPO_ROOT)
        return True
    except ValueError:
        return False


def _table(rows: List[dict]) -> str:
    out = ["| document | page | degrees | ink |", "|---|---:|---:|---:|"]
    for r in rows:
        deg = "n/a (blank)" if r["degrees"] is None else f"{r['degrees']:+.2f}"
        out.append(f"| {r['doc_id']} | {r['page']} | {deg} | {r['ink_fraction']} |")
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="skew_report", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("documents", nargs="+", help="PDF or image paths")
    parser.add_argument("--out", required=True,
                        help="report JSON to write — must be OUTSIDE the repository")
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI)
    parser.add_argument("--max-angle", type=float, default=DEFAULT_MAX_ANGLE)
    args = parser.parse_args(argv)

    out = Path(args.out)
    if _is_inside_repo(out):
        print(f"error: {out} is inside the repository — write the skew report to scratch")
        return 2

    rows: List[dict] = []
    for name in args.documents:
        path = Path(name)
        if not path.is_absolute():
            path = REPO_ROOT / path
        if not path.exists():
            print(f"error: no such document: {path}")
            return 2
        print(f"measuring {path.name} …", flush=True)
        rows.extend(measure_document(path, dpi=args.dpi, max_angle=args.max_angle))

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"dpi": args.dpi, "max_angle": args.max_angle,
         "convention": "degrees = counter-clockwise rotation that would level the page; "
                       "measurement only, nothing is deskewed",
         "pages": rows}, indent=2), encoding="utf-8")

    print()
    print(_table(rows))
    measured = [abs(r["degrees"]) for r in rows if r["degrees"] is not None]
    if measured:
        print(f"\n{len(rows)} page(s) · max |skew| = {max(measured):.2f} deg · "
              f"pages over 1 deg: {sum(1 for m in measured if m > 1.0)}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
