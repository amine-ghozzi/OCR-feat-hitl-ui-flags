# Design note — Highlight a located cell in the Source Document snapshot

**Status:** ✅ IMPLEMENTED (2026-06-18) on branch `feat/hitl-ui-flags`. Docling
cell geometry is captured at extraction (`docling_extractor._table_geometry` →
`metadata['table_boxes']`), resolved against the grid via
`review_view.resolve_source_boxes`, and drawn as PIL rectangles in
`app.render_source_view`. Verified end-to-end on Bilan 1: boxes land 1:1 on the
image (Docling page size == pixel size for scans → no scaling). Triggered by both
📍 Locate (focus_targets) and clicking a grid cell (onCellClicked → `source_sel`,
kept out of the grid key so it can't remount the grid / break editing).

**All supported formats (2026-06-18):** image (1:1), **PDF** (Tier-3 ensemble
carries the Tier-1 Docling geometry forward; page render at 150 DPI scaled by
img_px/page_pts; `page_no`-filtered), and **DOCX** (extract_from_docx OCRs each
embedded screenshot via extract_from_image → per-image geometry tagged with
`image_index`; the source view overlays each embedded image). Verified: boxes
land on the correct cell+column on the real PDF (EF CLB) and DOCX (Etat résultat)
samples. Only a digital PDF accepted at Tier-0 (no Docling pass) has no geometry
→ no box. Known latent risk: column index is positional, so a canonical table
whose columns diverge from the Docling layout could mis-align by a column.

---
_Original design note (2026-06-09):_

## Goal
When a reviewer clicks **📍 Locate** on a failed validation check, the relevant
cell(s) already highlight in the editable grid. Extend that so the **same cell is
boxed on the "Source Document" image** (the original scan/PDF page shown in the HITL
app), so the reviewer can compare the extracted value against the original at a glance.

The grid Locate already computes precise targets — `ValidationResult.locate` is a list
of `{label, hint, columns}` (one entry per relevant row, naming the value columns the
check uses). The source overlay should be driven by the **same targets** so the two
views stay in sync. See `src/hitl/review_view.py` (`decorate_for_grid`) and
`src/hitl/app.py` (`_check_targets`, `render_source_view`).

## Feasibility verdict
**Achievable, including on scanned documents.** The coordinates exist upstream but are
currently discarded:

- **Docling** (the OCR path your scans take) — confirmed in the installed version:
  `docling_core.types.doc.document.TableCell` exposes
  `bbox`, `row_span`, `col_span`, `start_row_offset_idx`, `end_row_offset_idx`,
  `start_col_offset_idx`, `end_col_offset_idx`, `text`. So **every cell has a bbox AND
  its grid position**. `TableData` = `{table_cells, num_rows, num_cols}`. The `TableItem`
  also carries `.prov` (page number + page-level bbox). Today `docling_extractor.py`
  reads only `prov.confidence`, never the geometry.
- **Tier-0 digital PDFs** (PyMuPDF) — `tab.bbox` + word geometry are read for row
  reconstruction (`pipeline.py` `_reconstruct_rows_by_geometry`) but not stored.

Nothing in the data model carries coordinates: `CanonicalCell/Row/Table`
(`src/accounting/canonical_model.py`) and `ExtractionResult` (`src/pipeline.py`) hold
text only (`CanonicalTable.page_range` is the sole position hint). The Source Document
panel (`src/hitl/app.py` `render_source_view`, ~lines 300-346) renders the **full page**
via PyMuPDF `get_pixmap(dpi=150)` and **discards the image bytes** after `st.image`.

## Approach (recommended: Docling-bbox overlay; works on scans)
1. **Capture geometry at extraction.** In `src/extraction/docling_extractor.py`, when a
   table is exported (`export_to_dataframe`), also walk `table.data.table_cells` and
   build a geometry map aligned to the **DataFrame the grid shows**, keyed by
   `(row_idx, col_idx)` (use `start_row_offset_idx`/`start_col_offset_idx`), storing
   `{bbox, page_no, coord_origin}`. Keep page size (points) for scaling. Verify the
   `BoundingBox.coord_origin` (TOPLEFT vs BOTTOMLEFT) — Docling often uses BOTTOMLEFT;
   PyMuPDF render is TOPLEFT, so a `y` flip against page height may be needed.
2. **Thread + serialize.** Add a parallel structure on `ExtractionResult` (e.g.
   `metadata['table_boxes'][i]`, kept index-aligned with `result.tables[i]`), so it
   survives without plumbing through the canonical reorder/grouping. Persist it in the
   saved validation JSON if the source view must work after reload.
3. **Map a Locate target → bbox.** Given a `{label, columns}` target: resolve the
   column index by header name, the row index by label-text match (reuse the grid's
   normalized-label matching from `review_view._norm`), then look up the cell bbox.
4. **Render the overlay.** In `render_source_view`, when a focus target is active:
   render the page (already done), convert bbox points → pixels (`px = pt * 150/72`),
   apply the coord-origin flip if needed, and draw a rectangle (PIL `ImageDraw` on the
   pixmap, or an HTML/canvas overlay) before `st.image`. Crop-to-region + zoom is a nice
   enhancement. Cache the rendered page in `session_state` keyed by (file, page) to
   avoid re-rendering every interaction.

### Tier-0 (digital PDF) fallback
Docling geometry only exists when a Docling tier produced the result. For Tier-0 digital
PDFs (selectable text layer), skip stored boxes and lazily call
`page.search_for(cell_raw_text)` (and/or the row label) at HITL time to get rectangles,
then feed the **same overlay renderer**. For image-only/scanned docs this returns
nothing — rely on the Docling path. Degrade gracefully (no box → grid-only Locate).

## Risks / unknowns to validate before building
- **Coordinate origin & scale**: confirm Docling `coord_origin` and that the page used
  for bbox equals the page rendered at 150 DPI (multi-page / cropped tables).
- **DataFrame ↔ Docling cell alignment**: `export_to_dataframe` may merge headers /
  spanning cells, so `(row_idx, col_idx)` offsets might not line up 1:1 with the grid.
  May need to match by cell text as a tie-breaker.
- **Reorders**: align geometry to `result.tables[i]` (what the grid renders), NOT the
  canonical model, to avoid the label-reorder/grouping plumbing.
- **Scope**: extractor + result schema + serialization + a new image-overlay renderer +
  tests. Bigger than the grid Locate work. Land Docling-capture + overlay first behind
  the regression gate, then the Tier-0 search fallback.

## Where the hooks already are
- Locate targets: `ValidationResult.locate` (`src/accounting/validator.py`),
  populated in VNC/Var%/Row-Sum/Balance/CR/Field-Type checks.
- Target plumbing in UI: `_check_targets`, `_focus_key`, `st.session_state.focus_targets`
  in `src/hitl/app.py`.
- Source render to extend: `render_source_view` in `src/hitl/app.py`.
- Cell/row matching helpers to reuse: `review_view._norm`, `_cell_num`, `_close`.
