"""Pure presentation logic for the HITL review grid + validation panel.

No Streamlit / st_aggrid imports — everything here is plain pandas so it can be
unit-tested in isolation. The Streamlit app (``app.py``) consumes these outputs:
``decorate_for_grid`` annotates the editable DataFrame with hidden ``*__style``
companion columns (per-cell CSS), a ``→ Map`` mapping-confidence column, and a
``__focus`` flag; the validation helpers classify / sort / humanize the V1–V4
checks. See BENCHMARK_REPORT.md §3.4 for the "ambiguity basis" concept.
"""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import pandas as pd

try:  # the only cross-package import — the canonical set of assumption bases
    from accounting.french_number import ASSUMPTION_BASES
except Exception:  # pragma: no cover - defensive for odd import paths
    ASSUMPTION_BASES = frozenset(
        {"single_sep_thousands", "single_sep_long_run", "comma_decimal_hint"}
    )

# Cell background colours (match the legacy CSS palette in app.py).
YELLOW = "#fff3cd"   # risky parse / footing mismatch / recomputed % / weak mapping
RED = "#f8d7da"      # dual-tier value conflict
INDIGO = "#e2e3f3"   # value scale rests on a corpus assumption / inference
FOCUS = "#cfe2ff"    # row the reviewer asked to "locate"
TEAL = "#d1ecf1"     # cell corrected by the reviewer this session (D16)
RESOLVED = "#d4f4dd" # cell auto-resolved by a validation identity (E2)
CONFIRMED = "#c3e6cb"# reviewer confirmed the value as-is (re-entered, unchanged)

STYLE_SUFFIX = "__style"
FOC_SUFFIX = "__foc"        # per value column: True → outline this cell (Locate)
MAP_COL = "→ Map"          # "→ Map"
MAP_STYLE_COL = "Map" + STYLE_SUFFIX
PARSED_COL = "≈ Parsed"    # D6: read-only parsed values of assumption-flagged cells
FOCUS_COL = "__focus"      # whole-row outline (checks with no specific column)
SCROLL_COL = "__scroll"    # row has any focus (row or cell) → scroll into view

# Meta columns that are never editable value cells.
_META_COLS = {"label", "label_raw", "canonical_term", "note", "notes", MAP_COL,
              PARSED_COL, FOCUS_COL, SCROLL_COL}


def _norm(text: Any) -> str:
    """Accent-strip, lowercase, keep alphanumerics only — for label/column match."""
    s = "" if text is None else str(text)
    decomposed = unicodedata.normalize("NFKD", s)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", stripped.lower())


def _cell_css(flag: Optional[str], basis: Optional[str]) -> str:
    """CSS background for one cell from its flag + parse basis (worst wins)."""
    if flag == "red":
        return RED
    if flag == "yellow":
        return YELLOW
    # E2: an identity-resolved cell must stay visually distinct — without this
    # branch a resolved basis falls through to default green and the auto-
    # mutation becomes invisible in the grid.
    if basis and str(basis).startswith("resolved_by"):
        return RESOLVED
    if basis in ASSUMPTION_BASES:
        return INDIGO
    return ""


def _mapping_badge(match_type: Optional[str], conf: Optional[float]) -> tuple[str, bool]:
    """Return (badge text, is_low_confidence) for a row's label→canonical mapping."""
    mt = match_type or "unrecognized"
    if mt in ("exact", "resolved"):
        return "✓", False                       # ✓
    if mt == "fuzzy":
        c = conf or 0.0
        return f"≈{c:.2f}", c < 0.85             # ≈0.87
    if mt == "header":
        return "", False
    label = {"grouped": "grouped", "custom": "custom"}.get(mt, "—")  # —
    return f"⚠ {label}", True                    # ⚠ grouped


def _cell_num(value: Any) -> Optional[float]:
    """Best-effort parse of a displayed cell to a float, for focus disambiguation.

    Tolerant of French formatting (spaces/NBSP as thousands, comma decimal, a
    trailing ``%``, parenthesised/trailing-minus negatives). Returns ``None`` when
    the cell has no parseable number — disambiguation simply falls back to first match.
    """
    s = str(value).strip()
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")") or s.rstrip().endswith("-")
    s = s.replace("(", "").replace(")", "").replace("%", "")
    s = s.replace("\xa0", "").replace(" ", "").rstrip("-").strip()
    # If exactly one comma and no dot, treat comma as decimal; else commas/dots group.
    if s.count(",") == 1 and s.count(".") == 0:
        s = s.replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if (neg and v > 0) else v


def _close(a: Optional[float], b: Optional[float]) -> bool:
    """Numeric match for focus disambiguation — relative tolerance with an abs floor."""
    if a is None or b is None:
        return False
    return abs(a - b) <= max(0.5, abs(b) * 0.01)


def _label_col(df: pd.DataFrame) -> Optional[str]:
    """Best guess at the row-label column (named 'label', else first text column)."""
    for c in df.columns:
        if _norm(c) == "label":
            return c
    return df.columns[0] if len(df.columns) else None


def _fmt_parsed(v: Any) -> str:
    """Human format for a parsed numeric (French-style space grouping)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f == int(f):
        return f"{int(f):,}".replace(",", " ")
    return f"{f:,.3f}".replace(",", " ")


# ---------------------------------------------------------------------------
# Source-document overlay: map a focus target → cell rectangle(s) on the page,
# using the Docling geometry captured at extraction (metadata['table_boxes']).
# ---------------------------------------------------------------------------

def _drow_matches_headers(drow0_cells: list, cols: list) -> bool:
    """True if Docling row 0 holds the column HEADERS (so df row N = Docling row
    N+1). Compares the row-0 cell texts to the DataFrame column names."""
    if not drow0_cells or not cols:
        return False
    head = {_norm(c.get("text")) for c in drow0_cells if _norm(c.get("text"))}
    named = [_norm(c) for c in cols if _norm(c)]
    if not head or not named:
        return False
    hits = sum(1 for c in named if c in head)
    return hits >= max(1, len(named) // 2)


def resolve_source_boxes(focus_targets, tables, table_boxes, page_no=None,
                         image_index=None) -> list:
    """Map focus targets → source-image rectangles using captured Docling geometry.

    Returns ``[{l, t, r, b, page_w, page_h, page_no, image_index}]`` in PAGE
    coordinates (the caller scales to pixels). Each target is
    ``{label, columns, row?, hint?}`` — the same shape the grid Locate uses; an
    explicit ``row`` (positional df index) pins the exact cell (disambiguates
    repeated labels like several "Amortissements"/"VNC"), otherwise the row is
    found by label-text match. ``page_no`` (PDF page) / ``image_index`` (DOCX
    embedded image) filter to the currently-shown page. Degrades to [] when
    geometry is missing (the overlay simply doesn't draw)."""
    boxes = []
    targets = focus_targets or []
    tables = tables or []
    table_boxes = table_boxes or []
    for i, df in enumerate(tables):
        g = table_boxes[i] if i < len(table_boxes) else None
        if not g or not g.get("cells"):
            continue
        if page_no is not None and g.get("page_no") not in (None, page_no):
            continue
        if image_index is not None and g.get("image_index") not in (None, image_index):
            continue
        cells = g["cells"]
        cols = list(df.columns)
        offset = 1 if _drow_matches_headers([c for c in cells if c.get("row") == 0], cols) else 0
        by_rc, label_to_drow = {}, {}
        for c in cells:
            by_rc.setdefault((c.get("row"), c.get("col")), c)
            if c.get("col") == 0:
                label_to_drow.setdefault(_norm(c.get("text")), c.get("row"))
        for t in targets:
            if isinstance(t.get("row"), int):
                drow = t["row"] + offset
                # Guard a positional target against the WRONG table: if it carries
                # a label, the row-0 cell at drow must match it (else skip).
                lbl = t.get("label")
                if lbl:
                    c0 = by_rc.get((drow, 0))
                    if not c0 or _norm(c0.get("text")) != _norm(lbl):
                        continue
            else:
                drow = label_to_drow.get(_norm(t.get("label")))
            if drow is None:
                continue
            col_idxs = [cols.index(c) for c in (t.get("columns") or []) if c in cols] or [0]
            for ci in col_idxs:
                c = by_rc.get((drow, ci))
                if c:
                    boxes.append({"l": c["l"], "t": c["t"], "r": c["r"], "b": c["b"],
                                  "page_w": g.get("page_w"), "page_h": g.get("page_h"),
                                  "page_no": g.get("page_no"),
                                  "image_index": g.get("image_index")})
    return boxes


def _page_transform(transform, page_no):
    """The per-page de-rotation record for *page_no*, or None.

    ``pages`` is keyed by int where the record is built, but in API mode the
    metadata travels as JSON and the keys come back as strings — look both up,
    otherwise the overlay silently loses the record on exactly the deployment
    OCR-6 is about.
    """
    pages = (transform or {}).get("pages") or {}
    if page_no is None or not pages:
        return None
    rec = pages.get(page_no)
    if rec is None:
        try:
            rec = pages.get(str(page_no), pages.get(int(page_no)))
        except (TypeError, ValueError):
            rec = None
    return rec if isinstance(rec, dict) else None


def derotated_page_size(transform, page_no):
    """``(width, height)`` of the de-rotated page WP-2.1 extracted, or None.

    The page area is the CropBox when one was promoted to MediaBox, else the
    MediaBox; a 90/270 de-rotation swaps the two dimensions. The caller compares
    this with the page it actually rendered to decide whether the overlay
    geometry is already in the right frame.
    """
    rec = _page_transform(transform, page_no)
    if not rec:
        return None
    try:
        rotation = int(rec.get("rotation") or 0) % 360
    except (TypeError, ValueError):
        return None
    page_box = (rec.get("cropbox") or rec.get("mediabox")) if rec.get("promoted") \
        else rec.get("mediabox")
    if not page_box or len(page_box) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in page_box)
    except (TypeError, ValueError):
        return None
    w, h = abs(x1 - x0), abs(y1 - y0)
    if not w or not h:
        return None
    return (h, w) if rotation in (90, 270) else (w, h)


def invert_orientation(boxes, transform, page_no, page_w=None, page_h=None) -> list:
    """Map extraction-frame boxes back into the STORED page frame (OCR-6).

    WP-2.1 runs Docling against a de-rotated COPY, so ``table_boxes`` are in the
    de-rotated frame: the frame of the page as ``/Rotate`` *displays* it, with
    the CropBox as the page area when one was promoted. This is the inverse —
    it maps those boxes onto a render of the ORIGINAL that ignores ``/Rotate``
    and covers the full MediaBox:

    * 180 → point reflection about the page centre;
    * 90 / 270 → axis swap (a wide-short box becomes tall-narrow), with the
      offsets that put it back on the right edge;
    * a promoted CropBox → the constant offset from the MediaBox corner, since
      the de-rotated page covered only the CropBox.

    Identity when there is no transform, when *page_no* is not in the record
    (an unrotated page, or one ``skipped_pages`` left alone — nothing was
    changed, so nothing is to be undone), or when the recorded rotation is 0.

    ``page_w`` / ``page_h`` are the size of the frame *boxes* are expressed in
    (the de-rotated page); each box's own ``page_w``/``page_h`` is used when they
    are None. The returned boxes carry the STORED page size, so ``_overlay_boxes``
    keeps scaling page→pixels correctly.

    **Measured at bf6967f — `render_source_view` does NOT need this for its PDF
    preview.** ``fitz.Page.get_pixmap()`` honours ``/Rotate`` *and* the CropBox,
    and ``Page.remove_rotation()`` is appearance-preserving, so rendering the
    ORIGINAL gives byte-for-byte the de-rotated page (verified on fis_innov —
    18 pages of ``/Rotate 270``, overlay drawn on p5 — and synthetically for 180,
    inherited 90, and both with a promoted CropBox). The app calls this only when
    the render it produced is *not* in the de-rotated frame; see
    ``app._boxes_in_render_frame``.
    """
    boxes = list(boxes or [])
    rec = _page_transform(transform, page_no)
    if not rec or not boxes:
        return boxes
    try:
        rotation = int(rec.get("rotation") or 0) % 360
    except (TypeError, ValueError):
        return boxes
    if rotation not in (90, 180, 270):
        return boxes

    promoted = bool(rec.get("promoted"))
    mediabox = rec.get("mediabox")
    page_box = (rec.get("cropbox") or mediabox) if promoted else mediabox
    if not page_box or len(page_box) != 4:
        return boxes
    try:
        x0, y0, x1, y1 = (float(v) for v in page_box)
    except (TypeError, ValueError):
        return boxes
    stored_w, stored_h = abs(x1 - x0), abs(y1 - y0)
    if not stored_w or not stored_h:
        return boxes

    # A promoted CropBox means the de-rotated page covered the CROP area only;
    # a MediaBox-framed render shows more page, so every box shifts by the
    # CropBox corner. PDF space is bottom-up, the overlay frame is TOP-LEFT, so
    # the vertical offset is measured from the MediaBox TOP (my1), not my0.
    off_x = off_y = 0.0
    out_w, out_h = stored_w, stored_h
    if promoted and mediabox and len(mediabox) == 4:
        try:
            mx0, my0, mx1, my1 = (float(v) for v in mediabox)
        except (TypeError, ValueError):
            mx0 = my0 = mx1 = my1 = None
        if mx0 is not None:
            off_x, off_y = x0 - mx0, my1 - y1
            out_w, out_h = abs(mx1 - mx0), abs(my1 - my0)

    def _to_stored(dx, dy):
        if rotation == 180:
            return stored_w - dx, stored_h - dy
        if rotation == 90:  # display = stored turned 90 deg clockwise
            return dy, stored_h - dx
        return stored_w - dy, dx  # 270: turned 90 deg counter-clockwise

    # The de-rotated page's own size, in the record's units (points).
    derot_w, derot_h = ((stored_h, stored_w) if rotation in (90, 270)
                        else (stored_w, stored_h))

    out = []
    for b in boxes:
        bw = page_w or b.get("page_w") or derot_w
        bh = page_h or b.get("page_h") or derot_h
        sx = derot_w / bw if bw else 1.0
        sy = derot_h / bh if bh else 1.0
        try:
            corners = [_to_stored(float(b["l"]) * sx, float(b["t"]) * sy),
                       _to_stored(float(b["r"]) * sx, float(b["b"]) * sy)]
        except (KeyError, TypeError, ValueError):
            out.append(b)
            continue
        xs = [c[0] + off_x for c in corners]
        ys = [c[1] + off_y for c in corners]
        out.append({**b, "l": min(xs), "t": min(ys), "r": max(xs), "b": max(ys),
                    "page_w": out_w, "page_h": out_h})
    return out


def decorate_for_grid(
    flat_df: pd.DataFrame,
    ct_dict: Optional[dict],
    focus_label: Optional[str] = None,
    focus_hint: Optional[float] = None,
    focus_targets: Optional[list[dict]] = None,
    edited_cells: Optional[set] = None,
    confirmed_cells: Optional[set] = None,
    confirmed_maps: Optional[set] = None,
) -> tuple[pd.DataFrame, list[str], dict[str, int]]:
    """Annotate the editable ``flat_df`` with per-cell style companions + a mapping
    column, aligning to ``ct_dict`` (a ``CanonicalTable.to_dict()``) **by row label**.

    Returns ``(grid_df, value_cols, summary)``. ``grid_df`` = ``flat_df`` + hidden
    ``<col>__style`` columns + ``→ Map`` / ``Map__style`` + ``__focus``. Cells that
    cannot be aligned get no colour (never wrong). Per-cell colour is used when the
    flat column names align to canonical cell keys (Tier-3 records); otherwise the
    whole aligned row is coloured by its worst flag (robust to Tier-0/1 reorders).

    Locate: ``focus_targets`` is a list of ``{"label", "hint"}`` rows to highlight —
    a check may point at several (Field-Type aggregates, Balance/CR total rows). A
    SINGLE target resolves to ONE row (its ``hint`` value disambiguates rows that
    share a label, else first match); with MULTIPLE targets each label highlights
    every matching row. ``focus_label``/``focus_hint`` are a back-compat shorthand
    for a single target.

    D16: ``edited_cells`` is a set of ``(row_idx, column_name)`` the reviewer has
    corrected this session — those cells render TEAL (human ground truth wins over
    every flag colour, including red/indigo) and drop out of the assumption count.

    D6: rows whose cells carry an assumption/resolved basis get a read-only
    ``≈ Parsed`` companion column showing the canonical parsed value(s), so the
    reviewer sees what the pipeline actually read without losing the OCR text.
    """
    grid = flat_df.copy()
    label_col = _label_col(grid)
    value_cols = [c for c in grid.columns if _norm(c) not in _META_COLS and c != label_col]
    summary = {"yellow": 0, "red": 0, "low_conf": 0, "assumption": 0,
               "edited": 0, "resolved": 0, "confirmed": 0, "n_rows": len(grid)}
    edited_cells = edited_cells or set()

    # Index canonical rows by normalized label (first-unused wins on collision).
    rows = (ct_dict or {}).get("rows", []) if isinstance(ct_dict, dict) else []
    canon_by_label: dict[str, list[dict]] = {}
    for r in rows:
        canon_by_label.setdefault(_norm(r.get("raw_text")), []).append(r)
    used: dict[str, int] = {}

    # NB: ``label_col`` can legitimately be "" (unnamed OCR label column), which is
    # falsy — must test ``is not None``, or alignment + focus silently break.
    lbl_norms = [_norm(grid.iloc[i][label_col]) if label_col is not None else ""
                 for i in range(len(grid))]
    style_cols = {c: [""] * len(grid) for c in value_cols}
    map_text: list[str] = [""] * len(grid)
    map_style: list[str] = [""] * len(grid)
    focus_flags: list[bool] = [False] * len(grid)

    for i in range(len(grid)):
        lbl_norm = lbl_norms[i]

        bucket = canon_by_label.get(lbl_norm) or []
        k = used.get(lbl_norm, 0)
        crow = bucket[k] if k < len(bucket) else (bucket[-1] if bucket else None)
        if bucket:
            used[lbl_norm] = k + 1
        if not crow:
            continue

        # Mapping badge (skip pure header rows). A reviewer can confirm a weak
        # mapping as-is (→ Map cell) — it then renders CONFIRMED (green) and the
        # badge flips ⚠ → ✓ so the change is visible, not just a colour swap.
        if not crow.get("is_header"):
            badge, low = _mapping_badge(crow.get("match_type"), crow.get("match_confidence"))
            map_text[i] = badge
            if low and i in (confirmed_maps or set()):
                map_text[i] = badge.replace("⚠", "✓")
                map_style[i] = CONFIRMED
                summary["confirmed"] += 1
            elif low:
                map_style[i] = YELLOW
                summary["low_conf"] += 1

        cells = crow.get("cells") or {}
        sig_by_key = {
            _norm(key): _cell_css(c.get("flag"), c.get("basis")) for key, c in cells.items()
        }
        matched_any = any(_norm(c) in sig_by_key for c in value_cols)
        _ORDER = {"": 0, RESOLVED: 1, INDIGO: 2, YELLOW: 3, RED: 4}
        row_worst = ""
        for c in cells.values():
            css = _cell_css(c.get("flag"), c.get("basis"))
            if _ORDER.get(css, 0) > _ORDER.get(row_worst, 0):
                row_worst = css

        for c in value_cols:
            css = sig_by_key.get(_norm(c), "")
            if not css and not matched_any:
                css = row_worst  # column names didn't align → colour the whole row
            style_cols[c][i] = css

    # Reviewer confirmed the value AS-IS (re-entered the same value to vouch for a
    # flagged cell) — clears the flag to a calm "confirmed" colour. An actual edit
    # below (TEAL) is a stronger signal and overrides it.
    for (r, c) in (confirmed_cells or set()):
        if isinstance(r, int) and 0 <= r < len(grid) and c in style_cols:
            style_cols[c][r] = CONFIRMED

    # D16: reviewer-corrected cells — human ground truth wins over every flag.
    for (r, c) in edited_cells:
        if isinstance(r, int) and 0 <= r < len(grid) and c in style_cols:
            style_cols[c][r] = TEAL

    # Tally AFTER all overrides so counts reflect what is actually shown.
    for c in value_cols:
        for css in style_cols[c]:
            if css == YELLOW:
                summary["yellow"] += 1
            elif css == RED:
                summary["red"] += 1
            elif css == INDIGO:
                summary["assumption"] += 1
            elif css == TEAL:
                summary["edited"] += 1
            elif css == RESOLVED:
                summary["resolved"] += 1
            elif css == CONFIRMED:
                summary["confirmed"] += 1

    # Locate highlight. Each entry is ONE relevant row + the columns to outline in it
    # (a vertical sum → one column across several entries; VNC/Var% → a few columns in
    # one entry; empty columns → outline the whole row). Back-compat: focus_label/hint
    # is a single column-less entry.
    targets = focus_targets if focus_targets is not None else (
        [{"label": focus_label, "hint": focus_hint}] if focus_label else []
    )
    valcol_by_norm = {_norm(c): c for c in value_cols}
    cell_foc = {c: [False] * len(grid) for c in value_cols}
    scroll_flags = [False] * len(grid)

    def _resolve(t: dict) -> Optional[int]:
        # Pick the ONE row this target points at:
        #   0. ``row`` positional index — most reliable: the report is recomputed on the
        #      SAME tables the grid renders, so the index aligns. Guarded by a label
        #      check so it's table-safe (focus targets are applied to every table's grid)
        #      and self-corrects if the index is somehow off. Survives a bad cell whose
        #      text was auto-corrected away (where ``match`` can't confirm it).
        #   1. ``match`` {column: exact text} — confirm the row by a surviving cell text.
        #   2. ``hint`` numeric — the row whose cells contain the check's value.
        #   3. else the first label match.
        tnorm = _norm(t.get("label"))
        ridx = t.get("row")
        if isinstance(ridx, int) and 0 <= ridx < len(grid) and (not tnorm or lbl_norms[ridx] == tnorm):
            return ridx
        cands = [i for i in range(len(grid)) if lbl_norms[i] == tnorm] if tnorm else []
        match = t.get("match") or {}
        if match:
            mm = {valcol_by_norm[_norm(c)]: str(txt).strip()
                  for c, txt in match.items() if _norm(c) in valcol_by_norm}
            if mm:
                pool = cands or list(range(len(grid)))  # content is unique → search all
                best_row, best_hits = None, 0
                for i in pool:
                    hits = sum(1 for gc, txt in mm.items()
                               if str(grid.iloc[i][gc]).strip() == txt)
                    if hits > best_hits:
                        best_row, best_hits = i, hits
                return best_row if best_hits > 0 else None  # unconfirmed → don't guess
        hint = t.get("hint")
        if hint is not None:
            for i in cands:
                if any(_close(_cell_num(grid.iloc[i][c]), hint) for c in value_cols):
                    return i
        return cands[0] if cands else None

    for t in targets:
        if not (t.get("label") or t.get("match")):
            continue
        row = _resolve(t)
        if row is None:
            continue
        # Outline every column the check flagged in this (confirmed) row — even cells
        # whose text was since cleaned/emptied; they belong to the same finding.
        hit_cols = [valcol_by_norm[_norm(c)] for c in (t.get("columns") or [])
                    if _norm(c) in valcol_by_norm]
        if hit_cols:
            for c in hit_cols:
                cell_foc[c][row] = True              # outline these specific cells
        else:
            focus_flags[row] = True                  # no usable column → outline the row
        scroll_flags[row] = True

    # The highlight is drawn as an OUTLINE (cell border / row border) — we deliberately
    # do NOT recolour cells, so each keeps its own flag colour (yellow/red/indigo).

    for c in value_cols:
        grid[c + STYLE_SUFFIX] = style_cols[c]
        grid[c + FOC_SUFFIX] = cell_foc[c]
    grid[MAP_COL] = map_text
    grid[MAP_STYLE_COL] = map_style
    grid[FOCUS_COL] = focus_flags
    grid[SCROLL_COL] = scroll_flags
    return grid, value_cols, summary


# ---------------------------------------------------------------------------
# Validation-panel helpers (pure)
# ---------------------------------------------------------------------------

# D14 re-tier (post-D1/D2b the accounting checks are trustworthy):
#   meta       — structural notes, not accounting rules (Tables Found, Segmentation)
#   prominent  — the accounting identities + integrity checks (V1 core + V2)
#   advisory   — genuinely heuristic/informational (footing, V3 anomalies, V4 ratios)
_META = ("tables found", "segmentation")
_ADVISORY = ("row sum", "magnitude", "negative", "null ca", "proportion",
             "fonds de roulement", "tresorerie nette", "trésorerie nette", "ratio")
_SEV_RANK = {"CRITICAL": 0, "ERROR": 1, "WARNING": 2, "INFO": 3}


# --- D11: formal rule catalog -------------------------------------------------

_RULES_PATH = (Path(__file__).resolve().parents[1]
               / "accounting" / "rules" / "validation_rules.json")

# check_name pattern (lowercased substring) → rule_id. Ordered: first hit wins.
_CHECK_TO_RULE = (
    ("balance", "V1-001"),
    ("vnc", "V1-002"),
    ("cr equation", "V1-003"),
    ("row sum", "V1-004"),
    ("var%", "V1-005"),
    ("variation", "V1-005"),
    ("field type", "V1-006"),
    ("resultat net match", "V2-001"),
    ("tresorerie match", "V2-002"),
    ("report a nouveau", "V2-003"),
    ("report à nouveau", "V2-003"),
    ("negative", "V3-001"),
    ("null ca", "V3-002"),
    ("magnitude", "V3-003"),
    ("proportion", "V3-004"),
    ("fonds de roulement", "V4-001"),
    ("tresorerie nette", "V4-002"),
    ("trésorerie nette", "V4-002"),
)


@lru_cache(maxsize=1)
def load_rule_catalog() -> tuple:
    """All rules from validation_rules.json (active or not), as a tuple of dicts."""
    try:
        with open(_RULES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return tuple(data.get("rules", []))
    except Exception:
        return tuple()


def rule_by_id(rule_id: str) -> Optional[dict]:
    for r in load_rule_catalog():
        if r.get("rule_id") == rule_id:
            return r
    return None


def rule_for_check(check: Any) -> Optional[dict]:
    """The formal rule a check implements, or None for structural/meta checks."""
    name = (getattr(check, "check_name", "") or "").lower()
    for pattern, rid in _CHECK_TO_RULE:
        if pattern in name:
            return rule_by_id(rid)
    return None


def rule_caption(rule: Optional[dict]) -> str:
    """One-line formal identity: 'V1-005 Variation Percentage · `formula` · tol ±0.5'."""
    if not rule:
        return ""
    parts = [f"{rule.get('rule_id')} {rule.get('rule_name')}"]
    if rule.get("formula"):
        parts.append(f"`{rule['formula']}`")
    tol = rule.get("tolerance")
    if tol:
        parts.append(f"tol ±{tol * 100:.1f}%" if tol < 1 else f"tol ±{tol}")
    return " · ".join(parts)


class CheckInfo:
    """Plain-language description of a validation check for the HITL UI."""
    __slots__ = ("title", "checks", "fix", "rule")

    def __init__(self, title: str, checks: str, fix: str, rule: Optional[dict] = None):
        self.title = title       # human title (jargon-free)
        self.checks = checks     # what this rule verifies (always shown)
        self.fix = fix           # what to do when it fails
        self.rule = rule         # D11: the formal rule dict (id/formula/tolerance)


def describe_check(check: Any) -> "CheckInfo":
    """Translate an internal check into a self-explaining (title, what, fix),
    tied to its formal rule (D11: rule ID + formula + tolerance)."""
    info = _describe_base(check)
    info.rule = rule_for_check(check)
    return info


def _describe_base(check: Any) -> "CheckInfo":
    name = getattr(check, "check_name", "") or ""
    label = getattr(check, "row_label", None)
    sfx = f" — {label}" if label else ""
    n = name.lower()
    if "balance" in n:
        return CheckInfo(
            "Balance sheet balances",
            "Total Assets must equal Liabilities + Equity.",
            "A row is mis-read or has a wrong sign — check the totals on both sides.")
    if n.startswith("vnc") or n.startswith("v1: vnc") or "vnc" in n:
        return CheckInfo(
            f"Net = Gross − Depreciation{sfx}",
            "For this asset line, Net value must equal Gross minus Amortisation.",
            "Check the Gross / Amortisation / Net cells on this row.")
    if "cr equation" in n:
        return CheckInfo(
            "Income statement adds up",
            "Net result must equal Operating + Financial − Tax.",
            "Check the operating, financial and tax result rows.")
    if "row sum" in n:
        return CheckInfo(
            f"Subtotal adds up{sfx}",
            "This total should equal the sum of the rows beneath it.",
            "Check the component rows feeding this subtotal (footing is a noisy check).")
    if "sign" in n and ("var%" in n or "variation" in n):
        return CheckInfo(
            f"Change % sign convention{sfx}",
            "The reported % magnitude matches; only the sign differs (the document "
            "likely reports against |prior year|).",
            "No action needed unless the sign matters for your use — verify the row if unsure.")
    if "var%" in n or "variation" in n:
        return CheckInfo(
            f"Year-over-year change %{sfx}",
            "The “%” column should equal (this year − last year) ÷ last year.",
            "Use 📍 Locate — it highlights the % cell and the two year cells it is "
            "computed from; verify those three against the source document.")
    if "field type" in n:
        return CheckInfo(
            "Cells contain valid numbers",
            "Every value cell should parse as a number, not garbled OCR text.",
            "Fix the flagged cells (examples below) in the grid above.")
    if "negative" in n:
        field = name.split("Negative", 1)[-1].strip() or (label or "this value")
        return CheckInfo(
            f"No impossible negative — {field}",
            f"“{field}” cannot be negative in a real statement.",
            "Check the sign — likely a stray OCR minus.")
    if "null ca" in n:
        return CheckInfo(
            "Revenue is present",
            "An operating company should report non-zero revenue (chiffre d'affaires).",
            "Likely an extraction miss — check the revenue row.")
    if "magnitude" in n:
        return CheckInfo(
            "Values are a plausible scale",
            "All figures should be within a reasonable range of each other.",
            "If one value looks ~1000× off, it's a unit/scale OCR error.")
    if "fonds de roulement" in n or "tresorerie" in n or "trésorerie" in n or n.startswith("v4"):
        return CheckInfo(
            "Working-capital ratio (informational)",
            "A financial-health indicator (long-term resources − fixed assets).",
            "Informational only — a negative value is a business signal, not an extraction error.")
    if "match" in n or n.startswith("v2"):
        return CheckInfo(
            "Cross-statement values agree",
            "A figure shared by two statements (e.g. net result) should match in both.",
            "Check that shared figure in each statement.")
    if "tables found" in n:
        return CheckInfo(
            "A table was extracted",
            "At least one table must be found in the document.",
            "If this fails the document didn't parse — re-process or check the source file.")
    if "segmentation" in n:
        return CheckInfo(
            "Statement type identified",
            "Each table should be classified (Bilan / Compte de résultat / …).",
            "If unknown, the layout wasn't recognised — verify the table headers.")
    # D11: the fallback must still be a real explanation, not a raw identifier.
    return CheckInfo(
        name or "Automatic check",
        getattr(check, "message", "")
        or "An automatic consistency check on the extracted figures.",
        "Review the referenced row against the source document (use 📍 Locate if shown).")


def is_meta(check: Any) -> bool:
    """A structural/meta note (Tables Found, Statement Segmentation) — not an
    accounting rule; never counted as a 'high-value check' (D14)."""
    name = getattr(check, "check_name", "").lower()
    return any(p in name for p in _META)


def is_advisory(check: Any) -> bool:
    """A genuinely heuristic/informational check (footing, V3 anomalies, V4
    ratios) — unless CRITICAL. Post-D1/D2b, Var% and Field-Type are trustworthy
    accounting checks and are NOT advisory (D14 re-tier)."""
    if getattr(check, "severity", "") == "CRITICAL":
        return False
    name = getattr(check, "check_name", "").lower()
    return any(p in name for p in _ADVISORY)


def classify_checks(checks: list) -> tuple[list, list, list]:
    """D14 split: (meta, prominent, advisory).

    ``meta`` are structural notes; ``prominent`` are the accounting checks that
    drive approval (V1 identities, field integrity, V2 cross-statement);
    ``advisory`` are heuristics/ratios. Skipped checks stay in their tier (the
    renderer shows them as Skipped, never as PASS).
    """
    meta = [c for c in checks if is_meta(c)]
    rest = [c for c in checks if not is_meta(c)]
    prominent = [c for c in rest if not is_advisory(c)]
    advisory = [c for c in rest if is_advisory(c)]
    return meta, prominent, advisory


def sort_by_severity(checks: list) -> list:
    """Failed FAMILIES first (CRITICAL→…), and entries of the same rule family
    stay ADJACENT — failures, then the aggregate/passing entries, then the
    skipped coverage notes — so e.g. the Var% aggregate and its "not
    verifiable" coverage entry render next to each other instead of being
    scattered by severity."""
    def _fam(c) -> str:
        r = rule_for_check(c)
        return r["rule_id"] if r else (getattr(c, "check_name", "") or "").lower()

    groups: dict[str, list] = {}
    for c in checks:
        groups.setdefault(_fam(c), []).append(c)

    def _group_key(k: str):
        g = groups[k]
        any_fail = any((not getattr(c, "passed", True)) and not getattr(c, "skipped", False)
                       for c in g)
        best_sev = min(_SEV_RANK.get(getattr(c, "severity", "WARNING"), 4) for c in g)
        return (not any_fail, best_sev, k)

    def _within_key(c):
        skipped = getattr(c, "skipped", False)
        failed = (not getattr(c, "passed", True)) and not skipped
        return (skipped, not failed,
                _SEV_RANK.get(getattr(c, "severity", "WARNING"), 4),
                getattr(c, "check_name", ""))

    out: list = []
    for k in sorted(groups, key=_group_key):
        out.extend(sorted(groups[k], key=_within_key))
    return out


# --- D5: before/after reconciliation pairing ----------------------------------

def pair_reports(before: Any, after: Any) -> dict[str, str]:
    """Pair two ValidationReports by ``check_name`` → transition per check.

    Transitions (for the BEFORE report's checks, which the UI renders):
      ``fixed``      — failed before, passes after  → "Confirmed by reconciliation ✓"
      ``still_fail`` — fails in both                → "Suspect ✗"
      ``regressed``  — passed before, fails after   → parsed values broke an identity
      ``ok``         — passes in both
      ``not_rerun``  — present before, absent after (couldn't be re-evaluated)
    Skipped results count as absent (a skip is not evidence either way).
    """
    def _state(checks: list) -> dict[str, bool]:
        out: dict[str, bool] = {}
        for c in checks:
            if getattr(c, "skipped", False):
                continue
            out[getattr(c, "check_name", "")] = bool(getattr(c, "passed", True))
        return out

    b = _state(getattr(before, "checks", []) or [])
    a = _state(getattr(after, "checks", []) or [])
    trans: dict[str, str] = {}
    for name, b_pass in b.items():
        if name not in a:
            trans[name] = "not_rerun"
        elif not b_pass and a[name]:
            trans[name] = "fixed"
        elif not b_pass and not a[name]:
            trans[name] = "still_fail"
        elif b_pass and not a[name]:
            trans[name] = "regressed"
        else:
            trans[name] = "ok"
    return trans


# --- D12: honest rule-coverage panel -------------------------------------------

# Why a rule produced no check, by rule id (used when the report has no match).
_NOT_RUN_REASON = {
    "V1-002": "no Brut/Amort/Net presentation detected in this document",
    "V1-003": "no Compte de Résultat table detected",
    "V1-005": "no usable %/variation column detected",
    "V2-001": "needs Bilan + Compte de Résultat in the same document",
    "V2-002": "needs Bilan + Flux de Trésorerie in the same document",
    "V2-003": "needs a bilan with N/N-1 Report-à-Nouveau + prior Résultat",
    "V3-002": "no Compte de Résultat table detected",
    "V3-004": "needs Bilan + Compte de Résultat (Créances/Stocks vs Chiffre d'affaires)",
    "V4-002": "needs Trésorerie Actif/Passif rows on the bilan",
}


def _verified_detail(live: list) -> str:
    """Honest passing-detail for a rule's live checks.

    A family validator (VNC, Variation %, Row Sums, Field Types) emits ONE
    aggregate result that summarises N rows, and lists individual rows only on
    failure — so a raw object count read "1 check(s) verified" even when the
    single roll-up verified many rows. Prefer the aggregate's own message (it
    already names the count AND the right noun: "All 6 … VNC identities verified",
    "All 4 footable subtotals foot ✓"); fall back to the object count when no
    aggregate carries stats."""
    aggs = [c for c in live
            if isinstance(getattr(c, "stats", None), dict)
            and c.stats.get("candidates")]
    if aggs:
        best = max(aggs, key=lambda c: c.stats.get("candidates", 0) or 0)
        msg = (getattr(best, "message", "") or "").strip()
        if msg:
            return msg
        verified = best.stats.get("verified") or best.stats.get("candidates")
        return f"{verified} verified"
    return f"{len(live)} check(s) verified"


def coverage(report: Any, found_types: set, standard: str = "NCT") -> dict:
    """D12: tiered status for EVERY rule in the catalog — ran/passed/failed/
    skipped/N-A/inactive — with per-tier counts. Pure + unit-testable.

    ``found_types`` = statement types detected in this document (e.g.
    ``{"bilan"}``). A rule whose ``applicable_statements`` don't intersect them
    is N/A; an inactive rule is shown as inactive; an active, applicable rule
    with no produced check is "not run" with an honest reason.
    """
    checks = list(getattr(report, "checks", []) or [])
    found = {t for t in (found_types or set()) if t and t != "unknown"}
    # bilan sub-types count as bilan for applicability
    if found & {"bilan_actif", "bilan_passif"}:
        found.add("bilan")

    by_rule: dict[str, list] = {}
    for c in checks:
        r = rule_for_check(c)
        if r:
            by_rule.setdefault(r["rule_id"], []).append(c)

    tiers: dict[str, dict] = {}
    for rule in load_rule_catalog():
        rid = rule["rule_id"]
        tier = rule.get("tier", "V?")
        entry = {"rule_id": rid, "name": rule.get("rule_name", rid),
                 "severity": rule.get("severity", "WARNING"), "detail": ""}

        if not rule.get("active", True):
            entry["status"] = "inactive"
        elif standard not in rule.get("standards", []):
            entry["status"] = "n/a"
            entry["detail"] = f"not part of the {standard} rule set"
        else:
            matched = by_rule.get(rid, [])
            live = [c for c in matched if not getattr(c, "skipped", False)]
            skipped = [c for c in matched if getattr(c, "skipped", False)]
            applicable = set(rule.get("applicable_statements", [])) & found
            if live:
                fails = [c for c in live if not getattr(c, "passed", True)]
                entry["status"] = "fail" if fails else "pass"
                if fails:
                    entry["detail"] = getattr(fails[0], "message", "")
                else:
                    entry["detail"] = _verified_detail(live)
            elif skipped:
                entry["status"] = "skipped"
                entry["detail"] = getattr(skipped[0], "message", "")
            elif found and not applicable:
                entry["status"] = "n/a"
                entry["detail"] = ("needs " +
                                   "/".join(rule.get("applicable_statements", [])) +
                                   f" — found {', '.join(sorted(found)) or 'none'}")
            else:
                entry["status"] = "not_run"
                entry["detail"] = _NOT_RUN_REASON.get(
                    rid, "no check produced for this document")

        tiers.setdefault(tier, {"rules": [], "counts": {}})["rules"].append(entry)

    for tier, block in tiers.items():
        counts: dict[str, int] = {}
        for e in block["rules"]:
            counts[e["status"]] = counts.get(e["status"], 0) + 1
        counts["ran"] = counts.get("pass", 0) + counts.get("fail", 0)
        counts["total"] = len(block["rules"])
        block["counts"] = counts
    return tiers


# --- D13/D14: override-adjusted scoring + honest quality suggestion ------------

def corrected_score(checks: list, eff_passed: list) -> Optional[float]:
    """Severity-weighted score with human overrides applied; skipped (D7) and
    INFO/meta-weightless checks excluded — the honest number for the headline."""
    total = 0
    passed = 0
    weights = {"CRITICAL": 3, "ERROR": 2, "WARNING": 1, "INFO": 0}
    for c, eff in zip(checks, eff_passed):
        if getattr(c, "skipped", False):
            continue
        w = weights.get(getattr(c, "severity", "WARNING"), 1)
        total += w
        if eff:
            passed += w
    return (passed / total) if total > 0 else None


def suggest_quality(checks: list, eff_passed: list, qcs_score: Optional[float]) -> int:
    """D13: quality radio default from the OVERRIDE-ADJUSTED, skip-excluded
    verdict. Returns the index into [Excellent, Good, Acceptable, Needs Correction].
    """
    crit_fail = err_fail = warn_fail = 0
    for c, eff in zip(checks, eff_passed):
        if getattr(c, "skipped", False) or eff:
            continue
        sev = getattr(c, "severity", "WARNING")
        if sev == "CRITICAL":
            crit_fail += 1
        elif sev == "ERROR":
            err_fail += 1
        elif sev == "WARNING":
            warn_fail += 1
    if crit_fail or err_fail:
        return 3                                   # Needs Correction
    if warn_fail:
        return 2                                   # Acceptable (minor notes)
    q = qcs_score or 0.0
    if q >= 0.85:
        return 0                                   # Excellent
    if q >= 0.75:
        return 1                                   # Good
    return 2                                       # Acceptable


# --- In-place corrections: the ③ Final and ② Before-validation views ------------

def resolution_entries(ct_dict: Optional[dict]) -> list[dict]:
    """The correction-loop actions recorded on a canonical table dict."""
    md = (ct_dict or {}).get("metadata") or {}
    return list(md.get("scale_resolutions") or [])


def apply_resolutions_inplace(flat_df: pd.DataFrame, ct_dict: Optional[dict]) -> tuple[pd.DataFrame, int]:
    """③ Final view: write each resolved/confirmed/inferred value INTO its cell
    (French-formatted) — corrections are in place; provenance stays visible via
    the mint/yellow cell colour (basis-driven) and the corrections summary.
    Returns ``(corrected_df, n_applied)``. Row/col come from the loop records;
    anything that doesn't align is skipped (never guessed)."""
    out = flat_df.copy()
    cols = {_norm(c): c for c in out.columns}
    n = 0
    for r in resolution_entries(ct_dict):
        try:
            row = int(r.get("row"))
            col = cols.get(_norm(r.get("col")))
            new = r.get("new_parsed")
            if col is None or new is None or not (0 <= row < len(out)):
                continue
            out.iloc[row, out.columns.get_loc(col)] = _fmt_parsed(new)
            n += 1
        except Exception:
            continue
    return out, n


def revert_resolutions_ct(ct_dict: Optional[dict]) -> dict:
    """② Before-validation view: the canonical dict with the correction loop
    UNDONE (parsed_value/basis restored from each action's recorded pre-state)
    so the view honestly shows what parsing alone produced — the cells the loop
    later fixed are indigo/ambiguous again."""
    import copy
    out = copy.deepcopy(ct_dict or {})
    rows = out.get("rows") or []
    for r in resolution_entries(out):
        try:
            row = rows[int(r["row"])]
        except Exception:
            continue
        for key, cell in (row.get("cells") or {}).items():
            if _norm(key) != _norm(r.get("col")):
                continue
            cell["parsed_value"] = r.get("old_parsed")
            cell["basis"] = r.get("old_basis")
            if r.get("kind") == "inferred":
                # pre-loop, the cell was empty/garbled — never green-confident
                cell["flag"] = "yellow" if str(cell.get("raw_value") or "").strip() else "green"
            break
    md = out.get("metadata")
    if isinstance(md, dict):
        md.pop("scale_resolutions", None)
        md.pop("resolution_iterations", None)
    return out


def parsed_view_frame(flat_df: pd.DataFrame, reverted_ct: Optional[dict]) -> pd.DataFrame:
    """② Before-validation view content: each numeric cell as the value parsing
    ALONE produced (pre-loop), French-formatted; unparsed cells keep their raw
    text. Positionally aligned to the canonical rows (caller guards lengths)."""
    out = flat_df.copy()
    rows = (reverted_ct or {}).get("rows") or []
    label_col = _label_col(out)
    valcols = {_norm(c): c for c in out.columns
               if _norm(c) not in _META_COLS and c != label_col}
    for i, rdict in enumerate(rows):
        if i >= len(out):
            break
        for key, cell in (rdict.get("cells") or {}).items():
            col = valcols.get(_norm(key))
            if col is None:
                continue
            pv = cell.get("parsed_value")
            if pv is not None:
                out.iloc[i, out.columns.get_loc(col)] = _fmt_parsed(pv)
    return out


# --- D16: in-memory correction diff --------------------------------------------

def diff_tables(prev: list, cur: list) -> list[dict]:
    """Cell-level diff between two lists of equally-shaped DataFrames.

    Returns ``[{"table", "row", "col", "label", "old", "new"}]`` — one entry per
    changed cell, positionally aligned (the grid never reorders rows). Tables
    whose shape changed are skipped (cannot attribute cell edits safely).
    """
    events: list[dict] = []
    for t, (p, c) in enumerate(zip(prev, cur)):
        if p is None or c is None or p.shape != c.shape or list(p.columns) != list(c.columns):
            continue
        lc = _label_col(p)
        for col in p.columns:
            pv, cv = p[col].values, c[col].values
            for r in range(len(p)):
                old = "" if pd.isna(pv[r]) else str(pv[r])
                new = "" if pd.isna(cv[r]) else str(cv[r])
                if old.strip() == new.strip():
                    continue
                label = str(p[lc].iloc[r]) if lc is not None else f"Row {r}"
                events.append({"table": t, "row": r, "col": str(col),
                               "label": label, "old": old, "new": new})
    return events


def humanize(check: Any) -> str:
    """Rewrite the most developer-ish check messages into plain reviewer language,
    keeping the numbers. Falls back to the original message."""
    name = getattr(check, "check_name", "") or ""
    msg = getattr(check, "message", "") or ""
    diff = getattr(check, "difference", None)
    diff_txt = f"{abs(diff):,.0f}" if isinstance(diff, (int, float)) else "the amount shown"
    if "Balance" in name:
        return f"Assets should equal Liabilities + Equity — they differ by {diff_txt}. A row is likely mis-read or mis-signed. ({msg})"
    if "CR Equation" in name:
        return f"Net result should equal Operating + Financial − Tax — off by {diff_txt}. ({msg})"
    if "Magnitude" in name:
        return f"Values span a very wide range — a unit/scale OCR error is possible. ({msg})"
    if name.startswith("VNC"):
        return f"Net should equal Brut − Amortissement for this row. {msg}"
    if name.startswith("Row Sum"):
        return f"This total should equal the sum of its rows. {msg}"
    return msg
