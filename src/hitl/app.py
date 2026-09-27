"""
FinAlze HITL Validation UI

Streamlit-based human-in-the-loop interface for:
- Viewing extracted data alongside source documents
- Editing and correcting OCR results
- Validating with accounting rules
- Approving/rejecting extractions
"""

import streamlit as st
import pandas as pd
import json
from pathlib import Path
from datetime import datetime
import sys
import io
import time

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline import FinAlzePipeline, PipelineConfig, ExtractionResult
from accounting.statement_segmenter import segment_tables
from accounting.validator import ValidationResult, ValidationReport
from hitl import review_view
from hitl.workflow import render_progress, render_raw_snapshot
from document_intake import DocumentRejected
from hitl.review_view import (
    STYLE_SUFFIX, FOC_SUFFIX, MAP_COL, MAP_STYLE_COL, FOCUS_COL, SCROLL_COL, INDIGO,
)

# AgGrid gives ONE grid that is both editable AND per-cell colourable (native
# st.data_editor cannot colour editable cells). Guarded so the app still loads
# (falling back to st.data_editor) if the optional component is unavailable.
try:
    from st_aggrid import AgGrid, GridOptionsBuilder, DataReturnMode, JsCode
    _HAS_AGGRID = True
except Exception:  # pragma: no cover
    _HAS_AGGRID = False

# Display name mappings for statement types
_STATEMENT_DISPLAY = {
    "bilan": "Bilan",
    "compte_resultat": "Compte de Resultat",
    "flux_tresorerie": "Flux de Tresorerie",
}
_SUB_TYPE_DISPLAY = {"actif": "Actif", "passif": "Passif"}


def _reconstruct_report(report_dict: dict) -> ValidationReport:
    """Reconstruct a ValidationReport from the dict stored in metadata."""
    report = ValidationReport()
    for check_data in report_dict.get("checks", []):
        vr = ValidationResult(
            check_name=check_data["name"],
            passed=check_data["passed"],
            skipped=check_data.get("skipped", False),
            severity=check_data.get("severity", "WARNING"),
            message=check_data.get("message", ""),
            expected=check_data.get("expected"),
            actual=check_data.get("actual"),
            difference=check_data.get("difference"),
            row_label=check_data.get("row_label"),
            locate=check_data.get("locate"),
            stats=check_data.get("stats"),
        )
        report.add_check(vr)
    report.compute_severity_weighted_score()
    return report


# Page configuration
st.set_page_config(
    page_title="FinAlze OCR Validator",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS for better styling
st.markdown("""
<style>
    .main-header {
        font-size: 2.5rem;
        font-weight: 700;
        color: #1f77b4;
        margin-bottom: 1rem;
    }
    .metric-card {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        padding: 1rem;
        border-radius: 10px;
        color: white;
    }
    .success-box {
        background-color: #d4edda;
        border: 1px solid #c3e6cb;
        border-radius: 5px;
        padding: 1rem;
        margin: 1rem 0;
    }
    .warning-box {
        background-color: #fff3cd;
        border: 1px solid #ffeeba;
        border-radius: 5px;
        padding: 1rem;
        margin: 1rem 0;
    }
    .error-box {
        background-color: #f8d7da;
        border: 1px solid #f5c6cb;
        border-radius: 5px;
        padding: 1rem;
        margin: 1rem 0;
    }
</style>
""", unsafe_allow_html=True)


def init_session_state():
    """Initialize session state variables."""
    if 'extraction_result' not in st.session_state:
        st.session_state.extraction_result = None
    if 'edited_tables' not in st.session_state:
        st.session_state.edited_tables = []
    # Cells the reviewer vouched for as-is (re-entered the same value) — per table
    # index → set of (row_idx, column_name). Clears the cell's risk flag.
    if 'confirmed_cells' not in st.session_state:
        st.session_state.confirmed_cells = {}
    # Weak label→canonical mappings the reviewer confirmed as-is (→ Map cell) —
    # per table index → set of row_idx. Clears the mapping's ⚠ low-confidence flag.
    if 'confirmed_maps' not in st.session_state:
        st.session_state.confirmed_maps = {}
    # The grid cell the reviewer last clicked → boxed on the source document.
    if 'source_sel' not in st.session_state:
        st.session_state.source_sel = None
    if 'validation_history' not in st.session_state:
        st.session_state.validation_history = []
    if 'pipeline' not in st.session_state:
        st.session_state.pipeline = None
    if 'current_file' not in st.session_state:
        st.session_state.current_file = None
    if 'last_processing_time' not in st.session_state:
        st.session_state.last_processing_time = None
    # D16: dynamic revalidation state — the as-displayed baseline (post auto-
    # correct), the snapshot at the last re-evaluate, the live report, and the
    # CorrectionEvent trail captured for learning.
    if 'baseline_tables' not in st.session_state:
        st.session_state.baseline_tables = []
    if 'reeval_snapshot' not in st.session_state:
        st.session_state.reeval_snapshot = None
    if 'validation_report_current' not in st.session_state:
        st.session_state.validation_report_current = None
    if 'correction_trail' not in st.session_state:
        st.session_state.correction_trail = []


def render_sidebar():
    """Render the sidebar with configuration options."""
    st.sidebar.markdown("## ⚙️ Configuration")
    
    # QCS Threshold sliders
    st.sidebar.markdown("### QCS Thresholds")
    tau_fast = st.sidebar.slider(
        "Tier 1 (Fast OCR)",
        min_value=0.5,
        max_value=1.0,
        value=0.75,
        step=0.05,
        help="Minimum QCS for Tier 1 to accept without escalation"
    )
    tau_vlm = st.sidebar.slider(
        "Tier 2 (VLM)",
        min_value=0.5,
        max_value=1.0,
        value=0.70,
        step=0.05,
        help="Minimum QCS for Tier 2 to accept without HITL"
    )
    
    # Preprocessing options
    st.sidebar.markdown("### Preprocessing")
    preprocess_resize = st.sidebar.toggle(
        "Resize Large Images",
        value=True,
        help="Resize images wider than 2000px for faster processing"
    )
    preprocess_grayscale = st.sidebar.toggle(
        "Convert to Grayscale",
        value=True,
        help="Convert to grayscale for faster OCR processing"
    )
    max_image_width = st.sidebar.number_input(
        "Max Image Width (px)",
        min_value=500,
        max_value=4000,
        value=2000,
        step=100,
        help="Maximum width before resizing"
    )
    
    # Accounting standard. NCT first / default — it is the only complete vertical
    # (full canonical nomenclature + validation identities). IFRS/SYSCOHADA get a
    # per-standard OCR term vocabulary but otherwise ride the NCT rails.
    st.sidebar.markdown("### Accounting Standard")
    standard = st.sidebar.selectbox(
        "Select Standard",
        options=["NCT", "IFRS", "SYSCOHADA"],
        index=0,
        help="NCT is fully supported (canonical nomenclature + validation). "
             "IFRS/SYSCOHADA currently apply standard-specific OCR term "
             "corrections only; label matching & validation use the NCT dictionary.",
    )
    
    # F2: API-client mode — the backend worker owns extraction; never
    # instantiate the local pipeline (keeps the thin UI container honest).
    import os
    if os.environ.get("FINALZE_API_URL"):
        st.sidebar.caption(f"🌐 API mode — extraction via {os.environ['FINALZE_API_URL']}")
        return tau_fast, tau_vlm, standard, preprocess_resize, preprocess_grayscale, max_image_width

    # Auto-apply: always keep pipeline in sync with current slider values
    current_config_key = f"{tau_fast}_{tau_vlm}_{standard}"
    if 'last_config_key' not in st.session_state:
        st.session_state.last_config_key = None

    # Recreate pipeline if config changed or pipeline doesn't exist
    if st.session_state.pipeline is None or st.session_state.last_config_key != current_config_key:
        config = PipelineConfig(
            tau_fast_high=tau_fast,
            tau_vlm_med=tau_vlm,
            accounting_standard=standard
        )
        st.session_state.pipeline = FinAlzePipeline(config)
        st.session_state.last_config_key = current_config_key
        st.sidebar.caption(f"✓ Thresholds: T1={tau_fast:.2f}, T2={tau_vlm:.2f}")

    return tau_fast, tau_vlm, standard, preprocess_resize, preprocess_grayscale, max_image_width


_SAMPLES_DIR = Path(__file__).parent.parent.parent / "Samples"


def _list_sample_files() -> list[Path]:
    """Files in ``Samples/``, or ``[]`` when it is absent or unreadable.

    ``Samples/`` is a developer convenience. The deployed UI container has no
    such bind mount (SEC-10: the corpus holds client financial statements and
    must never ride along in a deployment), so every caller must treat "no
    samples" as the normal case — never as an error, and never as a reason to
    stop rendering the uploader beside it. ``glob`` on a missing directory is
    silent, but a directory that exists and cannot be read raises ``OSError``;
    both collapse to an empty list here.
    """
    try:
        return sorted(p for p in _SAMPLES_DIR.glob("*") if p.is_file())
    except OSError:
        return []


def render_upload_section():
    """Render the file upload section."""
    st.markdown("### 📁 Upload Document")
    
    uploaded_file = st.file_uploader(
        "Upload a financial statement (PDF, DOCX, JPG, PNG)",
        type=["pdf", "docx", "jpg", "jpeg", "png"],
        help="Supported formats: PDF, DOCX, JPG, PNG"
    )
    
    # Or select from the Samples directory. It is a developer convenience and is
    # NOT mounted into the deployed UI container (SEC-10) — a missing or
    # unreadable directory must leave the uploader working, not raise.
    sample_files = _list_sample_files()
    if sample_files:
        st.markdown("**Or select from samples:**")
        sample_names = ["-- Select --"] + [f.name for f in sample_files]
        selected_sample = st.selectbox("Sample files", sample_names)

        if selected_sample != "-- Select --":
            return _SAMPLES_DIR / selected_sample

    return uploaded_file


def _api_client():
    """API-mode client with session-cached auth (F4/F5).

    Credential precedence: explicit ``FINALZE_API_TOKEN`` / ``FINALZE_API_KEY``
    env vars; else a dev login with the bootstrap admin credentials
    (``FINALZE_ADMIN_EMAIL/PASSWORD`` — compose passes the same defaults the
    api service bootstraps with). The JWT is cached in session state; a 401
    mid-session (expired token) clears it so the next call re-authenticates.
    Returns None when not in API mode.
    """
    import os
    api_url = os.environ.get("FINALZE_API_URL")
    if not api_url:
        return None
    from client.sdk import FinAlzeClient
    api_key = os.environ.get("FINALZE_API_KEY") or None
    token = os.environ.get("FINALZE_API_TOKEN") or st.session_state.get("api_token")
    client = FinAlzeClient(base_url=api_url, api_key=api_key, token=token)
    if not token and not api_key:
        email = os.environ.get("FINALZE_ADMIN_EMAIL", "admin@finalze.io")
        password = os.environ.get("FINALZE_ADMIN_PASSWORD", "changeme-admin-pw")
        try:
            st.session_state["api_token"] = client.login(email, password)
        except Exception as exc:
            client.close()
            raise RuntimeError(
                f"Could not authenticate against the API at {api_url}: {exc}. "
                "Set FINALZE_API_TOKEN / FINALZE_API_KEY, or make sure the "
                "bootstrap admin credentials (FINALZE_ADMIN_EMAIL/PASSWORD) "
                "match the api service."
            ) from exc
    return client


def process_document(file_input, preprocess_resize=True, preprocess_grayscale=True, max_image_width=2000, accounting_standard="NCT"):
    """Process the uploaded document through the pipeline."""
    import os as _os
    # F2: in API mode the backend extracts — never build the local pipeline.
    if st.session_state.pipeline is None and not _os.environ.get("FINALZE_API_URL"):
        st.session_state.pipeline = FinAlzePipeline()

    # Handle both file objects and paths
    if isinstance(file_input, Path):
        input_path = file_input
    else:
        # Stage the uploaded file in a user-writable temp dir. NOT the project
        # ``output/`` tree: in the container that path is the bind-mounted
        # ``./output`` (docker-compose), owned by the host user and NOT writable
        # by the non-root uid the UI runs as (10003) — which raised
        # ``PermissionError: [Errno 13]`` on upload. The system temp dir is
        # always writable by the running user, in-container and locally alike,
        # and the staged file only needs to be READ afterwards (SDK upload /
        # in-process pipeline / source-doc overlay re-reads).
        import tempfile
        temp_dir = Path(tempfile.gettempdir()) / "finalze_uploads"
        temp_dir.mkdir(parents=True, exist_ok=True)
        import uuid
        temp_path = temp_dir / (uuid.uuid4().hex + "_" + Path(file_input.name).name)
        temp_path.write_bytes(file_input.getvalue())
        input_path = temp_path
    
    st.session_state.current_file = input_path
    st.session_state.extraction_result = None
    st.session_state.edited_tables = []
    st.session_state.api_document_id = None
    st.session_state.workflow_events = []
    st.session_state.intake_error = None
    st.session_state.pop("table_segments", None)

    import os
    api_url = os.environ.get("FINALZE_API_URL")
    if api_url:
        # API mode is NON-BLOCKING: submit (fast upload) and let each Streamlit
        # rerun poll the job once (_render_api_pending). A blocking poll here
        # freezes the whole UI for the duration of extraction (cold-start can
        # exceed 6 minutes) and used to time out at 300 s on jobs that succeed.
        from client.sdk import FinAlzeError
        try:
            try:
                with _api_client() as _client:
                    sub = _client.submit(input_path, accounting_standard=accounting_standard)
            except (FinAlzeError, RuntimeError) as exc:
                if "401" not in str(exc):
                    raise
                st.session_state.pop("api_token", None)  # stale token → re-auth once
                with _api_client() as _client:
                    sub = _client.submit(input_path, accounting_standard=accounting_standard)
        except Exception as exc:
            st.error(f"Backend submission failed: {exc}")
            st.stop()
        st.session_state.api_pending = {
            "document_id": sub["document_id"],
            "file": str(input_path),
            "standard": accounting_standard,
            "submitted_at": time.time(),
        }
        st.rerun()  # the pending banner takes over; the page stays responsive

    # Local mode (default): run the pipeline in-process.
    with st.spinner(f"Processing {input_path.name} ({accounting_standard})..."):
        start_time = time.perf_counter()
        progress_area = st.empty()
        def emit(item):
            st.session_state.workflow_events.append(item)
            with progress_area.container():
                render_progress(st, st.session_state.workflow_events)
        try:
            result = st.session_state.pipeline.process_document(
                input_path,
                preprocess_resize=preprocess_resize,
                preprocess_grayscale=preprocess_grayscale,
                max_image_width=max_image_width,
                accounting_standard=accounting_standard,
                progress_callback=emit,
            )
        except DocumentRejected as exc:
            st.session_state.intake_error = str(exc)
            st.error(str(exc))
            st.stop()
        except Exception:
            st.session_state.intake_error = "Le traitement a échoué. Vérifiez le fichier et la disponibilité du moteur."
            st.error(st.session_state.intake_error)
            st.stop()
        elapsed_time = time.perf_counter() - start_time
        _adopt_extraction_result(result, elapsed_time)

    return result, elapsed_time


def _result_from_api_dict(d: dict) -> "ExtractionResult":
    """Rebuild a typed ExtractionResult from the worker's serialized job result
    (F3: canonical tables rehydrate into typed objects, or the flag grid and
    the dual validation pass silently degrade in API mode)."""
    canonical_tables = []
    try:
        from accounting.canonical_model import CanonicalTable
        # DLV-15: a ``None`` entry is a real, load-bearing placeholder (schema v2 keys
        # this list per STATEMENT) — rehydrate it as None, never skip it, or every
        # later statement resolves to the wrong table.
        canonical_tables = [
            CanonicalTable.from_dict(x) if x else None
            for x in (d.get("canonical_tables") or [])
        ]
    except Exception:
        canonical_tables = []
    return ExtractionResult(
        text=d.get("text", "") or "",
        tables=d.get("tables") or [],
        qcs_score=d.get("qcs_score") or 0.0,
        tier_used=d.get("tier_used") or 0,
        confidence_details=d.get("confidence_details") or {},
        metadata=d.get("metadata") or {},
        canonical_tables=canonical_tables,
    )


def _adopt_extraction_result(result, elapsed_time: float) -> None:
    """Make an extraction result the session's working state: build the
    editable tables (auto-corrections + the correction loop baked IN-PLACE),
    snapshot the baseline, and reset the review/correction-trail state."""
    st.session_state.last_processing_time = elapsed_time
    st.session_state.extraction_result = result
    st.session_state.workflow_events = (result.metadata or {}).get("workflow_events", [])

    # Convert tables to DataFrames for editing, with auto-correction applied
    st.session_state.edited_tables = []
    st.session_state.confirmed_cells = {}  # fresh document → no confirmations yet
    st.session_state.confirmed_maps = {}
    st.session_state.source_sel = None
    try:
        from accounting.validator import auto_correct_variation_percentages
        apply_correction = True
    except ImportError:
        apply_correction = False

    for table_data in result.tables:
        df = pd.DataFrame(table_data)
        # Apply auto-correction immediately so UI shows corrected values
        if apply_correction:
            df, _ = auto_correct_variation_percentages(df)
        st.session_state.edited_tables.append(df)

    # The working copy IS the final output: bake the correction loop's
    # resolved/inferred values INTO the cells (provenance stays visible via
    # the mint/yellow colours + the corrections summary). Guarded per table
    # on row-count alignment — a mismatched table is left untouched.
    for i in range(len(st.session_state.edited_tables)):
        cdict = _canonical_dict(result, i)
        rows = (cdict or {}).get("rows") or []
        if cdict and len(rows) == len(st.session_state.edited_tables[i]):
            corrected, _n = review_view.apply_resolutions_inplace(
                st.session_state.edited_tables[i], cdict)
            st.session_state.edited_tables[i] = corrected

    # D16: snapshot the as-displayed state and reset the correction loop —
    # user edits are measured against this baseline.
    st.session_state.baseline_tables = [df.copy() for df in st.session_state.edited_tables]
    st.session_state.reeval_snapshot = None
    st.session_state.validation_report_current = None
    st.session_state.correction_trail = []
    st.session_state.check_overrides = {}
    st.session_state.pop("focus_targets", None)
    st.session_state.pop("table_segments", None)


# Cold-start extraction (worker loading OCR models) can take 6-8 minutes; a
# warm worker finishes in ~1-2. The pending poll gives up only after this cap.
_API_JOB_CAP_S = 1200


@st.fragment(run_every="2s")
def _api_pending_fragment() -> None:
    """Non-blocking job tracking (API mode): a self-refreshing FRAGMENT that
    polls the backend once every 2 s and renders live status + cancel — only
    this banner reruns, never the whole page, so the UI stays responsive for
    the entire extraction (the old blocking poll froze the page for minutes
    and timed out at 300 s on jobs that succeed at ~400 s cold-start)."""
    pend = st.session_state.get("api_pending")
    if not pend:
        return

    elapsed = time.time() - pend["submitted_at"]
    doc_id = pend["document_id"]
    status = None
    error = None
    try:
        with _api_client() as _client:
            doc = _client.get_document(doc_id)
            job = doc.get("latest_job") or {}
            status = job.get("status")
            error = job.get("error")
            st.session_state.workflow_events = job.get("progress") or []
            if status == "done":
                d = _client.get_result(doc_id)
                d.setdefault("document_id", doc_id)
                result = _result_from_api_dict(d)
                st.session_state.api_document_id = d.get("document_id")
                st.session_state.api_pending = None
                _adopt_extraction_result(result, elapsed)
                st.toast(f"Extraction done in {elapsed:.0f}s — QCS {result.qcs_score:.2f}")
                st.rerun(scope="app")
    except Exception as exc:  # one failed poll is not fatal — show and retry
        error = str(exc)

    if status == "failed":
        st.session_state.api_pending = None
        st.session_state.intake_error = error or "Le traitement a échoué."
        st.error(st.session_state.intake_error)
        return
    if elapsed > _API_JOB_CAP_S:
        st.session_state.api_pending = None
        st.error(f"Gave up waiting after {int(elapsed)}s — check the worker "
                 f"(docker logs finalze-worker-1); the job may still complete.")
        return

    render_progress(st, st.session_state.get("workflow_events", []))
    if error:
        st.warning(f"Suivi temporairement indisponible : {error}")
    with st.container(border=True):
        c1, c2 = st.columns([5, 1])
        c1.markdown(
            f"⏳ **Extracting `{Path(pend['file']).name}` on the backend** — "
            f"{int(elapsed)}s elapsed · job status: `{status or 'queued'}`"
        )
        c1.caption("A cold worker loads OCR models first (6-8 min); warm runs "
                   "take 1-2 min. The rest of the page stays usable meanwhile.")
        if c2.button("Masquer le suivi", key="cancel_api_job",
                     help="Le traitement continue sur le serveur. Ce bouton masque seulement son suivi."):
            st.session_state.api_pending = None
            st.rerun(scope="app")


def _source_boxes(page_no=None, image_index=None):
    """Resolve the active focus targets → source-page rectangles (page coords),
    using the Docling geometry captured at extraction. ``page_no`` filters a PDF
    page; ``image_index`` a DOCX embedded image. [] when nothing to draw."""
    result = st.session_state.get("extraction_result")
    table_boxes = (getattr(result, "metadata", None) or {}).get("table_boxes") if result else None
    if not table_boxes:
        return []
    targets = list(st.session_state.get("focus_targets") or [])  # Locate
    sel = st.session_state.get("source_sel")                     # clicked cell
    if sel:
        targets.append({"row": sel.get("row"), "label": sel.get("label"),
                        "columns": sel.get("columns") or []})
    if not targets:
        return []
    tables = st.session_state.get("edited_tables") or list(getattr(result, "tables", []) or [])
    return review_view.resolve_source_boxes(
        targets, tables, table_boxes, page_no=page_no, image_index=image_index)


def _orientation_transform():
    """The orientation record WP-2.1/WP-2.2 left in the extraction metadata, or {}.

    OCR-6: in API mode this record is *all* the UI ever gets — the worker's
    de-rotated temp copy is long gone by the time the reviewer clicks Locate, so
    the overlay has to reason about the frame from metadata alone.
    """
    result = st.session_state.get("extraction_result")
    metadata = (getattr(result, "metadata", None) or {}) if result else {}
    return metadata.get("orientation_transform") or {}


def _image_was_exif_transposed(image_index=None):
    """True when WP-2.2 turned this image upright before OCR.

    The geometry in ``table_boxes`` is then in the UPRIGHT frame while the file
    on disk still holds the stored (sideways) pixel buffer — PIL never applies
    the EXIF Orientation tag on ``Image.open``. Measured on an orientation-6
    JPEG: the highlight landed outside the table entirely.
    """
    images = _orientation_transform().get("images") or []
    if not images:
        return False
    if image_index is not None:
        for rec in images:
            if rec.get("image_index") == image_index:
                return bool(rec.get("transposed"))
        return False
    return bool(images[0].get("transposed"))


def _aspects_match(w1, h1, w2, h2, tol=0.02):
    """Do two frames have the same shape (so one is a pure rescale of the other)?

    ``_overlay_boxes`` already absorbs a uniform scale; what it cannot absorb is
    a frame whose axes are swapped or reflected. Unknown sizes answer True —
    "no evidence of a mismatch" must never move a box.
    """
    if not (w1 and h1 and w2 and h2):
        return True
    a1, a2 = w1 / h1, w2 / h2
    return abs(a1 - a2) <= tol * max(a1, a2)


def _boxes_in_render_frame(boxes, page_no, render_w, render_h):
    """Put PDF overlay boxes in the frame of the page this view just rendered.

    OCR-6. ``table_boxes`` come from Docling running on WP-2.1's de-rotated COPY,
    so they are in the de-rotated frame, not the frame of the file on disk.

    MEASURED at bf6967f: they need no correction here. ``fitz.get_pixmap()``
    honours ``/Rotate`` *and* the CropBox, and ``Page.remove_rotation()`` is
    appearance-preserving, so the page rendered from the ORIGINAL is byte-for-byte
    the de-rotated page — checked on fis_innov (18 pages of ``/Rotate 270``; p5 is
    the first carrying table geometry) and synthetically for 180, inherited 90,
    and both again with a promoted CropBox.
    Inverting unconditionally here would BREAK an overlay that is already right.

    So this is a guard, not a correction: it compares the rendered page with the
    de-rotated page the record describes and only reaches for
    ``review_view.invert_orientation`` when they disagree — a caller that renders
    with the rotation stripped, or a future change to the render above. A 180°
    record is the one case the shapes cannot distinguish (both frames have the
    same size); the measured render honours it, so it is left alone.
    """
    if not boxes:
        return boxes
    transform = _orientation_transform()
    derotated = review_view.derotated_page_size(transform, page_no)
    if not derotated:
        return boxes  # unrotated page, or one `skipped_pages` left alone
    if _aspects_match(render_w, render_h, derotated[0], derotated[1]):
        return boxes
    return review_view.invert_orientation(
        boxes, transform, page_no, derotated[0], derotated[1])


def _overlay_boxes(pil_img, boxes):
    """Draw a highlight rectangle per box (page coords) on a copy of the image,
    scaling page→pixels. Returns the annotated copy (or the original if no boxes)."""
    if not boxes:
        return pil_img
    from PIL import ImageDraw
    img = pil_img.convert("RGB")
    draw = ImageDraw.Draw(img, "RGBA")
    iw, ih = img.size
    for b in boxes:
        pw = b.get("page_w") or iw
        ph = b.get("page_h") or ih
        sx, sy = iw / pw, ih / ph
        x0, y0, x1, y1 = b["l"] * sx, b["t"] * sy, b["r"] * sx, b["b"] * sy
        pad = 2
        draw.rectangle([x0 - pad, y0 - pad, x1 + pad, y1 + pad], fill=(255, 196, 0, 56))
        draw.rectangle([x0 - pad, y0 - pad, x1 + pad, y1 + pad], outline=(255, 130, 0, 255), width=3)
    return img


def _table_source_locator(i):
    """Where the i-th extracted table sits in the source: ``("page", n)`` for a
    PDF/image, ``("image", n)`` for a DOCX embedded image, or None.

    Docling converts each DOCX embedded image as its OWN single-page document,
    so ``page_no`` is 1 for *every* DOCX table — reading it alone labelled them
    all "· p.1", which is wrong for a format that has no pages. ``image_index``
    (tagged on in ``extract_from_docx``) is the real locator there, so it wins
    whenever present.
    """
    if i is None:
        return None
    result = st.session_state.get("extraction_result")
    tb = (getattr(result, "metadata", None) or {}).get("table_boxes") if result else None
    if not (tb and i < len(tb) and tb[i]):
        return None
    geom = tb[i]
    img_idx = geom.get("image_index")
    if img_idx is not None:
        return ("image", int(img_idx))
    page_no = geom.get("page_no")
    return ("page", int(page_no)) if page_no else None


def _table_source_page(i):
    """PDF page number (1-based) the i-th table sits on, else None.

    Thin wrapper over :func:`_table_source_locator` — DOCX now correctly yields
    None here instead of a bogus page 1.
    """
    loc = _table_source_locator(i)
    return loc[1] if loc and loc[0] == "page" else None


def _table_label(i):
    """Display label for the i-th table: 'Table 1 — Bilan (Actif) · p.1'
    (or '· img 2' for a DOCX embedded image)."""
    lab = f"Table {i + 1}"
    segs = st.session_state.get("table_segments") or []
    seg = segs[i] if i < len(segs) else None
    if seg and seg.statement_type in _STATEMENT_DISPLAY:
        lab += f" — {_STATEMENT_DISPLAY[seg.statement_type]}"
        if seg.sub_type in _SUB_TYPE_DISPLAY:
            lab += f" ({_SUB_TYPE_DISPLAY[seg.sub_type]})"
    loc = _table_source_locator(i)
    if loc:
        lab += f" · p.{loc[1]}" if loc[0] == "page" else f" · img {loc[1] + 1}"
    return lab


def _render_table_selector():
    """One selector that drives BOTH panels: pick a table → its source page
    (left) and just that table's grid (right). Returns the selected index, or
    None when there are no tables."""
    tables = st.session_state.get("edited_tables") or []
    n = len(tables)
    if n == 0:
        return None
    # Segment labels are also computed in render_extraction_view; ensure they
    # exist here so the selector can label the tables.
    segs = st.session_state.get("table_segments") or []
    if len(segs) != n:
        try:
            st.session_state.table_segments = _table_segments(tables)
        except Exception:
            st.session_state.table_segments = []
    if n == 1:
        return 0
    return st.selectbox(
        "Table", options=list(range(n)), format_func=_table_label,
        key="table_selector",
    )


def render_source_view(selected_idx=None):
    """Render the source document view (with a highlight box on the located /
    clicked cell when the Docling geometry is available).

    ``selected_idx`` links the source to the chosen table: for a PDF the page is
    driven by that table's geometry, so source and extraction stay in lockstep.
    """
    st.markdown("### 📄 Source Document")

    if st.session_state.current_file:
        file_path = st.session_state.current_file
        suffix = file_path.suffix.lower()

        if suffix in ['.jpg', '.jpeg', '.png']:
            # Filter to the selected table's page as the PDF branch does — an
            # image yielding several tables would otherwise draw every table's
            # boxes at once.
            _loc = _table_source_locator(selected_idx)
            boxes = _source_boxes(page_no=_loc[1] if _loc and _loc[0] == "page" else None)
            # OCR-6: WP-2.2 turns an EXIF-rotated image upright BEFORE OCR, so
            # `table_boxes` are in the UPRIGHT frame while `Image.open` hands back
            # the stored (sideways) buffer — PIL never applies the Orientation
            # tag. Measured on an orientation-6 JPEG: the highlight landed
            # outside the table entirely. Transpose the PREVIEW instead of
            # un-rotating the boxes, so the reviewer reads the page the way OCR
            # did and both live in one frame.
            upright = _image_was_exif_transposed()
            if boxes or upright:
                from PIL import Image, ImageOps
                img = Image.open(str(file_path))
                if upright:
                    img = ImageOps.exif_transpose(img) or img
                st.image(_overlay_boxes(img, boxes),
                         caption=file_path.name, use_container_width=True)
            else:
                st.image(str(file_path), caption=file_path.name, use_container_width=True)
        elif suffix == '.pdf':
            try:
                import fitz  # PyMuPDF
                pdf_doc = fitz.open(str(file_path))
                num_pages = len(pdf_doc)
                tbl_page = _table_source_page(selected_idx)
                if tbl_page and 1 <= tbl_page <= num_pages:
                    # Page follows the selected table — source and extraction
                    # stay in lockstep (no separate page picker needed).
                    page_num = tbl_page
                elif num_pages > 1:
                    page_num = st.selectbox(
                        "Page",
                        options=list(range(1, num_pages + 1)),
                        format_func=lambda x: f"Page {x} of {num_pages}",
                        key="pdf_page_selector",
                    )
                else:
                    page_num = 1
                page = pdf_doc[page_num - 1]
                pix = page.get_pixmap(dpi=150)
                boxes = _source_boxes(page_no=page_num)
                # OCR-6: `table_boxes` are in WP-2.1's de-rotated frame. The
                # pixmap above honours /Rotate and the CropBox, which is the same
                # frame — this checks that rather than assuming it, and only
                # re-maps the boxes if this render ever stops matching.
                boxes = _boxes_in_render_frame(
                    boxes, page_num, page.rect.width, page.rect.height)
                cap = f"{file_path.name} — Page {page_num}/{num_pages}"
                if boxes:
                    from PIL import Image
                    img = Image.open(io.BytesIO(pix.tobytes("png")))
                    st.image(_overlay_boxes(img, boxes), caption=cap, use_container_width=True)
                else:
                    st.image(pix.tobytes("png"), caption=cap, use_container_width=True)
                pdf_doc.close()
            except ImportError:
                st.warning("PyMuPDF not installed. Install with: `pip install PyMuPDF`")
                st.markdown(f"**File:** `{file_path}`")
            except Exception as e:
                st.error(f"Could not render PDF: {e}")
                st.markdown(f"**File:** `{file_path}`")
        elif suffix == '.docx':
            st.info("DOCX preview not available. Showing extracted images.")
            # Show the embedded image the SELECTED table came from, boxing the
            # located/clicked cell on it (image_index matches
            # extract_from_docx's order). Previously every image was stacked and
            # ``selected_idx`` ignored, so the reviewer had to scroll to find
            # which one lit up — the PDF branch has always stayed in lockstep.
            try:
                from docx import Document
                from PIL import Image
                loc = _table_source_locator(selected_idx)
                want = loc[1] if loc and loc[0] == "image" else None
                doc = Document(file_path)
                img_idx = 0
                for rel in doc.part.rels.values():
                    if "image" not in rel.reltype:
                        continue
                    if want is not None and img_idx != want:
                        img_idx += 1
                        continue          # not the selected table's image
                    image_data = rel.target_part.blob
                    cap = f"Embedded Image {img_idx + 1}"
                    boxes = _source_boxes(image_index=img_idx)
                    # OCR-6, same frame rule as the image branch above: an
                    # embedded image that WP-2.2 turned upright for OCR must be
                    # previewed upright too, or the boxes land off the page.
                    upright = _image_was_exif_transposed(image_index=img_idx)
                    if boxes or upright:
                        from PIL import ImageOps
                        img = Image.open(io.BytesIO(image_data))
                        if upright:
                            img = ImageOps.exif_transpose(img) or img
                        st.image(_overlay_boxes(img, boxes),
                                 caption=cap, use_container_width=True)
                    else:
                        st.image(image_data, caption=cap, use_container_width=True)
                    img_idx += 1
            except Exception as e:
                st.error(f"Could not load DOCX: {e}")


def _statements(result):
    """The statement inventory from the post-OCR classification stage, or []."""
    return (getattr(result, "metadata", None) or {}).get("statements") or []


def _table_segments(tables):
    """Statement labels for the table selector and the grid headings.

    Prefers the result's OWN inventory over re-deriving one. Re-classifying here
    lets the UI disagree with the payload it is displaying — the label a
    reviewer sees would come from a different pass than the ``statements`` block
    stored with the result — and costs a full ``fuzzy_match`` sweep over every
    row on each rerun. Falls back to classifying only for a pre-inventory result.
    """
    result = st.session_state.get("extraction_result")
    stmts = _statements(result)
    if not stmts:
        return segment_tables(tables)

    from accounting.statement_segmenter import StatementSegment

    by_table = {}
    for s in stmts:
        for table_idx in (s.get("table_indices") or []):
            by_table[table_idx] = s

    segments = []
    for i, df in enumerate(tables):
        s = by_table.get(i) or {}
        segments.append(StatementSegment(
            table_index=i,
            # A quarantined statement carries type None — surface it as unknown
            # so the selector still lists it rather than hiding it.
            statement_type=s.get("type") or "unknown",
            confidence=float(s.get("confidence") or 0.0),
            keyword_hits=0,
            df=df,
            sub_type=s.get("sub_type"),
        ))
    return segments


def _canonical_slot(result, table_idx):
    """Index into ``canonical_tables`` for a given TABLE index.

    ``canonical_tables`` is keyed per STATEMENT, while every caller here works
    in table indices (that is what the grid, the geometry overlay and the stored
    corrections are all keyed on). The statement's ``table_indices`` back-pointer
    is the bridge. Falls back to the identity mapping when no inventory is
    present, so a result produced before the statement stage still resolves.
    """
    stmts = _statements(result)
    if stmts:
        for slot, s in enumerate(stmts):
            if table_idx in (s.get("table_indices") or []):
                return slot
        # An inventory exists and this table is in none of it (a blank frame, or
        # one that produced no statement). It genuinely HAS no canonical data —
        # falling through to the positional index here would hand back a
        # different statement's table entirely.
        return None
    return table_idx


def _canonical_dict(result, i):
    """Canonical table for TABLE index ``i`` as a dict (to_dict), or None."""
    cts = getattr(result, "canonical_tables", None) or []
    slot = _canonical_slot(result, i)
    if slot is None or slot >= len(cts):
        return None
    ct = cts[slot]
    if ct is None:
        return None
    try:
        return ct.to_dict() if hasattr(ct, "to_dict") else ct
    except Exception:
        return None


def _canonical_by_table(result):
    """Yield ``(table_index, canonical_dict)`` for every canonical table.

    Callers label their output with a *table* index; iterating
    ``canonical_tables`` positionally would hand them a statement index instead
    once the two stop coinciding.
    """
    stmts = _statements(result)
    if stmts:
        for s in stmts:
            idx = (s.get("table_indices") or [None])[0]
            if idx is not None:
                yield idx, _canonical_dict(result, idx)
        return
    for i in range(len(getattr(result, "canonical_tables", None) or [])):
        yield i, _canonical_dict(result, i)


def _cell_style_js(style_col: str) -> "JsCode":
    # The flag colours are light pastels; on the dark theme the default light text
    # is invisible on them, so force dark text whenever a background is applied.
    return JsCode(
        "function(p){var s=p.data['%s'];"
        "return s?{'backgroundColor':s,'color':'#1a1a1a','fontWeight':'600'}:null;}" % style_col
    )


def _value_cell_style_js(col: str) -> "JsCode":
    """cellStyle for a value column: keep the flag-colour background AND add a blue
    OUTLINE when this exact cell is a Locate target — so highlighting points at the
    cell without disturbing its colour."""
    return JsCode(
        "function(p){"
        "var bg=p.data['%s'];var foc=p.data['%s'];var s={};"
        "if(bg){s['backgroundColor']=bg;s['color']='#1a1a1a';s['fontWeight']='600';}"
        "if(foc){s['border']='2px solid #0d6efd';s['boxShadow']='inset 0 0 0 2px #0d6efd';}"
        "return Object.keys(s).length?s:null;}" % (col + STYLE_SUFFIX, col + FOC_SUFFIX)
    )


def _render_aggrid_table(i, flat_df, canonical_dict, focus_targets=None, edited_cells=None,
                         confirmed_cells=None, confirmed_maps=None):
    """Render ONE table as an editable, per-cell-coloured AgGrid. Returns
    ``(edited_value_df, summary, confirm_cell)`` — the edited frame with
    companion/meta presentation columns stripped (so the save/validation contract
    is unchanged), plus a ``(row, col)`` the reviewer just confirmed as-is (or
    None; ``col`` is MAP_COL for a mapping confirm). The CALLER persists the edit
    BEFORE acting on confirm_cell, so a confirm-triggered rerun can never drop an
    in-progress edit."""
    grid_df, value_cols, summary = review_view.decorate_for_grid(
        flat_df, canonical_dict, focus_targets=focus_targets, edited_cells=edited_cells,
        confirmed_cells=confirmed_cells, confirmed_maps=confirmed_maps,
    )
    # AgGrid cannot bind a column whose field name is blank (the OCR label column is
    # often named "") — it renders empty cells. Give it a display name for the grid
    # and restore the original name on write-back so the save/validation contract is
    # unchanged.
    label_col = review_view._label_col(grid_df)
    display_label = label_col
    if label_col is not None and str(label_col).strip() == "":
        display_label = "Label"
        grid_df = grid_df.rename(columns={label_col: display_label})

    # Hidden transport columns: __confirm (onCellEditingStopped stamps "row|colId"
    # when a cell is left UNCHANGED) and __select (onCellClicked stamps the clicked
    # cell, to box it on the source document). Both ride back in resp.data and are
    # never written to the saved tables.
    grid_df = grid_df.copy()
    grid_df["__confirm"] = ""
    grid_df["__select"] = ""

    gb = GridOptionsBuilder.from_dataframe(grid_df)
    # All data cells are editable (a reviewer must be able to fix garbled OCR
    # labels/notes, not only values); the derived → Map column is read-only.
    gb.configure_default_column(editable=True, resizable=True, sortable=False, filter=False, width=130)
    for c in grid_df.columns:
        if c.endswith(STYLE_SUFFIX) or c.endswith(FOC_SUFFIX) or c in (FOCUS_COL, SCROLL_COL, "__confirm", "__select"):
            gb.configure_column(c, hide=True)
    # The row-label column is the most important — pin it left, widen it, wrap long
    # labels so the reviewer can always read which row a flag/mapping refers to.
    if display_label is not None:
        gb.configure_column(display_label, editable=True, pinned="left", width=260, wrapText=True)
    for c in value_cols:
        gb.configure_column(c, editable=True, width=150, cellStyle=_value_cell_style_js(c))
    if MAP_COL in grid_df.columns:
        # Editable ONLY so the same confirm gesture (double-click + Enter, no
        # change) fires on a weak mapping. Edits to this derived badge are never
        # written back (it is not a flat_df column), so the value can't be
        # corrupted — it regenerates each render.
        gb.configure_column(MAP_COL, editable=True, width=150, pinned="right",
                            cellStyle=_cell_style_js(MAP_STYLE_COL))
    gb.configure_grid_options(
        # Located row with NO specific column: a strong blue OUTLINE only — no
        # background/text recolour, so cells keep their flag colour. (Checks that name
        # a column outline the individual cells instead — see _value_cell_style_js.)
        getRowStyle=JsCode(
            "function(p){return p.data['%s']?{'border':'2px solid #0d6efd','boxShadow':'inset 0 0 0 2px #0d6efd'}:null;}"
            % FOCUS_COL
        ),
        # Scroll the first located row (row- OR cell-level) into view.
        onFirstDataRendered=JsCode(
            "function(p){p.api.forEachNode(function(n){if(n.data['%s']){p.api.ensureNodeVisible(n,'middle');}});}"
            % SCROLL_COL
        ),
        # Same-value confirm: when editing stops with NO change, stamp "row|colId"
        # into the hidden __confirm cell. setDataValue fires cellValueChanged → the
        # grid returns → Python reads __confirm (and event_data) and marks the cell
        # confirmed. A real edit (valueChanged) is untouched here.
        onCellEditingStopped=JsCode(
            "function(e){if(!e.valueChanged && e.column.getColId()!=='__confirm'){"
            "e.node.setDataValue('__confirm', String(e.rowIndex)+'|'+e.column.getColId());}}"
        ),
        # Click a cell → stamp it into __select so the source-document panel boxes
        # the matching cell. Kept OUT of the grid cache key, so this never remounts
        # the grid (a mid-double-click remount would break editing).
        onCellClicked=JsCode(
            "function(e){var c=e.column.getColId();"
            "if(c!=='__confirm'&&c!=='__select'){"
            "e.node.setDataValue('__select', String(e.rowIndex)+'|'+c);}}"
        ),
    )
    resp = AgGrid(
        grid_df,
        gridOptions=gb.build(),
        allow_unsafe_jscode=True,
        data_return_mode=DataReturnMode.AS_INPUT,
        update_on=["cellValueChanged"],
        theme="streamlit",
        height=min(420, 70 + 30 * len(grid_df)),
        # The key embeds the actual highlight pattern so a "Locate" click (or a change
        # in what should be lit) forces a fresh grid — st_aggrid otherwise keeps cached
        # state and ignores the new focus columns. Rebuilt from edited_tables, so
        # committed edits are preserved. D16: the edited-cell pattern is included for
        # the same reason — committed corrections must repaint (teal) immediately.
        key=f"grid_{i}_{_highlight_key(grid_df, value_cols)}_{_edits_key(edited_cells)}_{_edits_key(confirmed_cells)}_{_edits_key(confirmed_maps)}",
    )
    out = pd.DataFrame(resp.data)
    # Robust write-back: start from the ORIGINAL flat_df (preserves the exact column
    # set + names for the save/validation contract) and overwrite each original
    # column with AgGrid's edited values — including the label (read under its
    # display name) and notes. A column AgGrid happened to drop simply keeps its
    # original value, so nothing is ever lost. ``__confirm`` is NOT a flat_df column,
    # so the confirm stamp never reaches the saved tables.
    edited = flat_df.copy()
    if len(out) == len(edited):
        for c in edited.columns:
            src = display_label if (c == label_col and display_label != label_col) else c
            if src in out.columns:
                edited[c] = out[src].values

    # Same-value confirm: resolve "row|colId" to a (row, ORIGINAL-col) cell. Primary
    # source is the __confirm stamp in resp.data; fall back to the cellEditingStopped
    # event_data (rowIndex + column.colId + valueChanged==False).
    def _to_cell(rrow, cid):
        try:
            rrow = int(rrow)
        except (TypeError, ValueError):
            return None
        if not cid or cid in ("__confirm", "__select"):
            return None
        real_col = label_col if (cid == display_label and display_label != label_col) else cid
        return (rrow, real_col)

    confirm_cell = None
    if "__confirm" in out.columns:
        for v in out["__confirm"]:
            if isinstance(v, str) and "|" in v:
                rs, _, cs = v.partition("|")
                confirm_cell = _to_cell(rs, cs) or confirm_cell
    if confirm_cell is None:
        ev = getattr(resp, "event_data", None)
        if isinstance(ev, dict) and ev.get("type") == "cellEditingStopped" \
                and ev.get("valueChanged") is False:
            col = ev.get("column") if isinstance(ev.get("column"), dict) else {}
            cid = col.get("colId") or (ev.get("colDef") or {}).get("field")
            confirm_cell = _to_cell(ev.get("rowIndex"), cid)

    # Clicked cell → which cell to box on the source document.
    select_cell = None
    if "__select" in out.columns:
        for v in out["__select"]:
            if isinstance(v, str) and "|" in v:
                rs, _, cs = v.partition("|")
                select_cell = _to_cell(rs, cs) or select_cell

    return edited, summary, confirm_cell, select_cell


def _styled_readonly(grid_df, value_cols):
    """E3 Before-view: a read-only pandas Styler carrying the same per-cell flag
    colours as the editable grid (built from the hidden ``__style`` columns)."""
    disp_cols = [c for c in grid_df.columns
                 if not c.endswith(STYLE_SUFFIX) and not c.endswith(FOC_SUFFIX)
                 and c not in (FOCUS_COL, SCROLL_COL)]
    view = grid_df[disp_cols]

    def _css(_):
        css = pd.DataFrame("", index=view.index, columns=view.columns)
        for c in value_cols:
            sc = c + STYLE_SUFFIX
            if sc in grid_df.columns and c in css.columns:
                css[c] = [f"background-color: {v}; color: #1a1a1a; font-weight: 600"
                          if v else "" for v in grid_df[sc]]
        if MAP_STYLE_COL in grid_df.columns and MAP_COL in css.columns:
            css[MAP_COL] = [f"background-color: {v}; color: #1a1a1a" if v else ""
                            for v in grid_df[MAP_STYLE_COL]]
        return css

    return view.style.apply(_css, axis=None)


_RES_KIND_ICON = {"resolved": (":green[✓ resolved]", "value corrected by the identity"),
                  "confirmed": (":green[✓ confirmed]", "assumption confirmed by the identity"),
                  "inferred": (":orange[⚠ inferred]", "missing cell reconstructed — verify")}


def _render_corrections_summary(result):
    """E3: every change between ① Before and ② After — E2 auto-resolutions
    (from the canonical tables' metadata) and the reviewer's manual edits."""
    auto = []
    for i, cdict in _canonical_by_table(result):
        md = (cdict or {}).get("metadata") or {}
        for r in (md.get("scale_resolutions") or []):
            auto.append({**r, "table": r.get("table", i)})
    edits = review_view.diff_tables(
        st.session_state.get("baseline_tables") or [],
        st.session_state.get("edited_tables") or [])
    if not auto and not edits:
        return
    with st.expander(
        f"Corrections summary — {len(auto)} validation-driven · {len(edits)} manual edit(s)",
        expanded=False,
    ):
        if auto:
            st.caption("Validation-driven (E2): ambiguous values arbitrated by the "
                       "accounting identities their confident neighbours pin down.")
            for r in auto:
                icon, gloss = _RES_KIND_ICON.get(r.get("kind"), (":gray[·]", ""))
                new_fmt = review_view._fmt_parsed(r.get("new_parsed"))
                st.markdown(
                    f"- {icon} **{r.get('label')}** [{r.get('col')}]: "
                    f"`{r.get('raw')}` → **{new_fmt}** — {r.get('rule')} ({r.get('note')})"
                )
        if edits:
            st.caption("Your manual corrections this session (saved on approval):")
            for e in edits:
                st.markdown(
                    f"- :blue[✎] **{e['label']}** [{e['col']}]: `{e['old']}` → `{e['new']}`"
                )


def _render_export_buttons():
    """D15: download the (edited) tables — one CSV per table + a single Excel
    workbook with one sheet per table. Exports exactly what the grid shows."""
    tables = st.session_state.get("edited_tables") or []
    if not tables:
        return
    stem = Path(str(st.session_state.current_file)).stem if st.session_state.current_file else "extraction"
    cols = st.columns(min(4, len(tables) + 1))
    for i, df in enumerate(tables):
        csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
        cols[i % len(cols)].download_button(
            f"⬇ Table {i + 1} (CSV)", data=csv_bytes,
            file_name=f"{stem}_table{i + 1}.csv", mime="text/csv",
            key=f"csv_dl_{i}", use_container_width=True,
        )
    try:
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            for i, df in enumerate(tables):
                # Excel sheet names: ≤31 chars, no special chars
                df.to_excel(writer, sheet_name=f"Table {i + 1}", index=False)
        cols[len(tables) % len(cols)].download_button(
            "⬇ All tables (Excel)", data=buf.getvalue(),
            file_name=f"{stem}_tables.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="xlsx_dl", use_container_width=True,
        )
    except Exception as e:  # openpyxl missing or sheet error — CSV still works
        st.caption(f":orange[Excel export unavailable: {e}]")


def render_extraction_summary():
    """Document-level QCS summary + breakdowns, rendered FULL-WIDTH above the
    source/extracted columns — so the editable grid lines up with the top of the
    source image instead of being pushed down by these panels."""
    result = st.session_state.extraction_result
    if result is None:
        return

    st.markdown("### 📊 Extracted Data")

    # Metrics row
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        # Show the score with its grade badge — and say WHICH score it is.
        # `metadata` is the authoritative copy (WP-2.7): at Tier 3 the grade in
        # `confidence_details` is inherited from whichever prior tier won and
        # describes a different extraction. RTE-10: at Tier 3 this number is the
        # ensemble's Q_table, not a QCS, and labelling it "QCS Score" invited
        # exactly the comparison it must not get.
        meta = result.metadata or {}
        qcs_grade = (
            meta.get('qcs_grade')
            or result.confidence_details.get('qcs_grade')
            or 'unknown'
        )
        score_kind = meta.get('score_kind') or ('q_table' if result.tier_used == 3 else 'qcs')
        grade_colors = {
            'excellent': '🟢',
            'good': '🟡',
            'fair': '🟠',
            'poor': '🔴',
            'unknown': '⚪',
        }
        grade_icon = grade_colors.get(qcs_grade, '⚪')
        if score_kind == 'q_table':
            label = "Q_table (consensus)"
            kind_help = (
                "Tier 3 stores the ensemble's table-agreement score here, not a QCS — "
                "a different quantity on a different scale."
            )
        else:
            label = "QCS Score"
            kind_help = "Quality Confidence Score (intrinsic + heuristic + semantic)."
        after_q = meta.get('qcs_after_quarantine')
        st.metric(
            label, f"{result.qcs_score:.2f} {grade_icon}",
            help=f"Grade: {qcs_grade.upper()} — {kind_help}",
        )
        st.caption(f"Grade: **{qcs_grade.upper()}**")
        if after_q is not None:
            st.caption(f"Recomputed after quarantine: {after_q:.2f}")
    with col2:
        st.metric("Tier Used", f"Tier {result.tier_used}")
    with col3:
        st.metric("Tables Found", len(result.tables))
    with col4:
        hitl_required = result.metadata.get('hitl_required', False)
        # metadata first (WP-2.7): it is the copy the worker persists and the
        # one the quarantine and the ensemble update.
        hitl_reason = (
            result.metadata.get('hitl_reason')
            or result.confidence_details.get('hitl_reason', '')
        )
        st.metric("HITL Required", "Yes" if hitl_required else "No")
        if hitl_required and hitl_reason:
            st.caption(f"Reason: {hitl_reason}")
    
    # Tier Attempts Display
    tier_attempts = result.confidence_details.get('tier_attempts', [])
    if tier_attempts:
        with st.expander("Tier QCS Breakdown", expanded=True):
            for attempt in tier_attempts:
                tier_num = attempt.get('tier', '?')
                tier_name = attempt.get('name', 'Unknown')
                qcs = attempt.get('qcs', 0)
                passed = attempt.get('passed', False)
                
                # Color-coded status
                if passed:
                    status = "✅ Passed"
                    color = "green"
                else:
                    status = "❌ Below threshold"
                    color = "red"
                
                col_tier, col_qcs, col_status = st.columns([2, 1, 2])
                with col_tier:
                    st.markdown(f"**Tier {tier_num}:** {tier_name}")
                with col_qcs:
                    st.markdown(f"QCS: **{qcs:.3f}**")
                with col_status:
                    st.markdown(f":{color}[{status}]")
    
    # OCR Correction Stats
    correction_stats = result.metadata.get('correction_stats', {})
    total_corr = correction_stats.get('total_corrections', 0)
    if total_corr > 0:
        with st.expander(f"OCR Corrections Applied ({total_corr})"):
            stat_items = [
                ('term_corrections', 'Term fixes'),
                ('camelcase_fixes', 'CamelCase splits'),
                ('missing_space_fixes', 'Space fixes'),
                ('ocr_char_fixes', 'Char substitutions'),
                ('underline_fixes', 'Underline fixes'),
            ]
            active = [(k, d) for k, d in stat_items if correction_stats.get(k, 0) > 0]
            if active:
                cols = st.columns(len(active))
                for j, (key, display) in enumerate(active):
                    cols[j].metric(display, correction_stats[key])

    # Confidence breakdown — curated (the raw dict is noisy), raw kept for debug.
    cd = result.confidence_details or {}
    with st.expander("Confidence breakdown"):
        grades = [("OCR", cd.get("ocr_grade")), ("Layout", cd.get("layout_grade")),
                  ("Parse", cd.get("parse_grade")), ("QCS", cd.get("qcs_grade"))]
        gcols = st.columns(len(grades))
        for (name, val), gc in zip(grades, gcols):
            gc.metric(name, str(val).upper() if val else "—")

        comp = [f"{lbl} {cd[k]:.2f}" for k, lbl in
                [("intrinsic_score", "Intrinsic"), ("heuristic_score", "Heuristic"),
                 ("semantic_score", "Semantic")] if isinstance(cd.get(k), (int, float))]
        if comp:
            st.caption("Component scores · " + " · ".join(comp))

        gff = cd.get("green_flag_failures") or result.metadata.get("green_flag_failures") or []
        if gff:
            st.caption("Quality-gate flags: " + " · ".join(f":red[{g}]" for g in gff))

        with st.expander("Raw confidence_details (debug)"):
            st.json(cd)


def render_extraction_view(selected_idx=None):
    """The editable extracted tables — the grid that sits BESIDE the source
    image (its summary/breakdowns are rendered separately, full-width above)."""
    result = st.session_state.extraction_result
    if result is None:
        st.info("No extraction result yet. Upload a document to start.")
        return

    # Editable tables — labelled by statement classification
    st.markdown("#### Valeurs traitées et corrections humaines")
    with st.expander("Lecture source brute — avant corrections", expanded=False):
        render_raw_snapshot(st, (result.metadata or {}).get("raw_snapshot"))

    # E3: three explicit views, ONE grid at a time (st.tabs would render every
    # AgGrid each rerun and hit the inactive-tab zero-width gotcha).
    #   ① raw OCR text · ② what parsing alone produced (pre-correction-loop,
    #   ambiguities indigo) · ③ the FINAL output: validation-driven corrections
    #   applied in place + your edits (editable, default).
    _V_FINAL = "③ Valeurs traitées / vos corrections"
    _V_PARSED = "② Valeurs interprétées"
    _V_RAW = "① Sortie extracteur (prétraitée)"
    view_mode = "final"
    if _HAS_AGGRID and (getattr(result, "canonical_tables", None) or []):
        _choice = st.segmented_control(
            "View", [_V_FINAL, _V_PARSED, _V_RAW],
            default=_V_FINAL, key="ba_view", label_visibility="collapsed",
            help="③ the final output — the correction loop's resolved/inferred "
                 "values written into the cells, plus your edits. ② what number "
                 "parsing alone produced (ambiguous cells indigo). ① the raw OCR "
                 "output after extractor preprocessing. For untouched readings, open the source snapshot above.",
        )
        view_mode = {"①": "raw", "②": "parsed"}.get(str(_choice or "")[:1], "final")

    flagged_banner = st.empty()  # filled after the loop with the cross-table total

    # Segment tables for classification labels (cached in session state)
    if 'table_segments' not in st.session_state or len(st.session_state.get('table_segments', [])) != len(st.session_state.edited_tables):
        try:
            st.session_state.table_segments = _table_segments(st.session_state.edited_tables)
        except Exception:
            st.session_state.table_segments = []

    focus_targets = st.session_state.get("focus_targets")
    totals = {"yellow": 0, "red": 0, "low_conf": 0, "assumption": 0}

    for i, df in enumerate(st.session_state.edited_tables):
        # Render ONLY the table picked by the selector — its source page shows
        # next to it (the other tables stay in edited_tables for validation /
        # export; they're just not drawn here).
        if selected_idx is not None and i != selected_idx:
            continue
        # Build label from segment classification
        label = f"Table {i+1}"
        seg = st.session_state.table_segments[i] if i < len(st.session_state.table_segments) else None
        if seg and seg.statement_type in _STATEMENT_DISPLAY:
            label += f" — {_STATEMENT_DISPLAY[seg.statement_type]}"
            if seg.sub_type in _SUB_TYPE_DISPLAY:
                label += f" ({_SUB_TYPE_DISPLAY[seg.sub_type]})"

        col_label, col_badge = st.columns([5, 1])
        with col_label:
            st.markdown(f"**{label}**")
        with col_badge:
            if seg and seg.statement_type != "unknown":
                badge_color = "green" if seg.confidence >= 0.6 else "orange"
                st.badge(f"{seg.confidence:.0%}", color=badge_color)

        cdict = _canonical_dict(result, i)
        if view_mode == "raw":
            # ① Raw extraction: the OCR text exactly as read, read-only. The
            # canonical dict is REVERTED so cells the loop later fixed show
            # their pre-correction colour (indigo/yellow), not the post state.
            try:
                raw_df = pd.DataFrame((result.tables or [])[i])
            except Exception:
                raw_df = df
            rev = review_view.revert_resolutions_ct(cdict)
            grid_df, vcols, summary = review_view.decorate_for_grid(raw_df, rev)
            st.dataframe(_styled_readonly(grid_df, vcols), use_container_width=True,
                         hide_index=True)
            for k in totals:
                totals[k] += summary.get(k, 0)
            continue
        if view_mode == "parsed":
            # ② Before validation: what number parsing ALONE produced —
            # pre-correction-loop values, ambiguous cells indigo, read-only.
            try:
                raw_df = pd.DataFrame((result.tables or [])[i])
            except Exception:
                raw_df = df
            rev = review_view.revert_resolutions_ct(cdict)
            parsed_df = review_view.parsed_view_frame(raw_df, rev)
            grid_df, vcols, summary = review_view.decorate_for_grid(parsed_df, rev)
            st.dataframe(_styled_readonly(grid_df, vcols), use_container_width=True,
                         hide_index=True)
            for k in totals:
                totals[k] += summary.get(k, 0)
            continue
        if _HAS_AGGRID and cdict is not None:
            edited_df, summary, confirm_cell, select_cell = _render_aggrid_table(
                i, df, cdict, focus_targets, edited_cells=_user_edited_cells(i),
                confirmed_cells=st.session_state.confirmed_cells.get(i, set()),
                confirmed_maps=st.session_state.confirmed_maps.get(i, set()))
            # PERSIST the edit FIRST — before any confirm/click-triggered rerun, so
            # an in-progress edit can never be dropped.
            st.session_state.edited_tables[i] = edited_df
            if confirm_cell:
                row, col = confirm_cell
                if col == MAP_COL:  # mapping confirm → clear the → Map ⚠ flag
                    mset = st.session_state.confirmed_maps.setdefault(i, set())
                    if row not in mset:
                        mset.add(row)
                        st.rerun()
                else:               # value-cell confirm
                    cset = st.session_state.confirmed_cells.setdefault(i, set())
                    if confirm_cell not in cset:
                        cset.add(confirm_cell)
                        st.rerun()
            # Clicked cell → box it on the source document. Stored separately from
            # focus_targets (Locate) and NOT in the grid key → no grid remount.
            if select_cell:
                row, col = select_cell
                label = str(edited_df.iloc[row][edited_df.columns[0]]) if row < len(edited_df) else ""
                sel = {"table": i, "row": row, "label": label,
                       "columns": [] if col == MAP_COL else [col]}
                if st.session_state.get("source_sel") != sel:
                    st.session_state.source_sel = sel
                    st.rerun()
            for k in totals:
                totals[k] += summary.get(k, 0)
            parts = []
            if summary["red"]:
                parts.append(f":red[{summary['red']} conflict]")
            if summary["yellow"]:
                parts.append(f":orange[{summary['yellow']} risky]")
            if summary["low_conf"]:
                parts.append(f"{summary['low_conf']} weak mapping(s)")
            if summary["assumption"]:
                parts.append(f"{summary['assumption']} assumption-based")
            if summary.get("resolved"):
                parts.append(f":green[{summary['resolved']} auto-resolved ✓]")
            if summary.get("edited"):
                parts.append(f":blue[{summary['edited']} corrected by you]")
            if summary.get("confirmed"):
                parts.append(f":green[{summary['confirmed']} confirmed as-is ✓]")
            st.caption(" · ".join(parts) if parts else ":green[no cells flagged]")
        else:
            # Fallback: no AgGrid / no canonical signal — plain editable grid.
            edited_df = st.data_editor(df, num_rows="dynamic", use_container_width=True, key=f"table_{i}")
            st.session_state.edited_tables[i] = edited_df

    # Cross-table flagged summary + legend
    n_flagged = totals["yellow"] + totals["red"]
    if _HAS_AGGRID:
        if n_flagged or totals["low_conf"] or totals["assumption"]:
            flagged_banner.warning(
                f"⚠ Review: {totals['red']} conflict · {totals['yellow']} risky · "
                f"{totals['low_conf']} weak mapping(s) · {totals['assumption']} assumption-based cell(s)."
            )
        else:
            flagged_banner.success("✅ No cells flagged for review.")
        with st.expander("Legend — what the colours mean"):
            _sw = "background:{bg};color:#1a1a1a;font-weight:600;padding:1px 8px;border-radius:3px"
            st.markdown(
                f"- <span style='{_sw.format(bg=review_view.RED)}'>red</span> "
                "— two extraction engines disagreed on this value (conflict).\n"
                f"- <span style='{_sw.format(bg=review_view.YELLOW)}'>yellow</span> "
                "— risky OCR recovery, footing mismatch, or a recomputed % — verify it.\n"
                f"- <span style='{_sw.format(bg=INDIGO)}'>indigo</span> "
                "— the value's scale rests on a corpus assumption / per-document inference "
                "(see the **≈ Parsed** column for what the pipeline read).\n"
                f"- <span style='{_sw.format(bg=review_view.RESOLVED)}'>green ✓</span> "
                "— an ambiguous value resolved by an arithmetic identity (VNC/balance).\n"
                f"- <span style='{_sw.format(bg=review_view.TEAL)}'>teal</span> "
                "— a cell you corrected this session (human ground truth).\n"
                f"- <span style='{_sw.format(bg=review_view.CONFIRMED)}'>green</span> "
                "— a value **or mapping** you **confirmed as-is**: double-click the cell "
                "(or the **→ Map** cell) and press Enter without changing it.\n"
                "- **→ Map** ⚠ — a weak label→canonical mapping (fuzzy/grouped/custom); "
                "confirm it the same way to clear the flag.",
                unsafe_allow_html=True,
            )

    # E3: what changed between ① and ② — auto-resolutions + manual edits.
    _render_corrections_summary(result)

    # D15: export the (edited) tables — CSV per table, one Excel workbook.
    _render_export_buttons()

    # Raw markdown dump lives ONLY in the ① Raw extraction view (the toggle
    # already provides the raw representation — no standalone expander).
    if view_mode == "raw":
        with st.expander("Raw extracted text (markdown)"):
            st.text_area("Markdown Output", result.text, height=300,
                         label_visibility="collapsed")


_SEV_COLOR = {"CRITICAL": "red", "ERROR": "orange", "WARNING": "blue", "INFO": "gray"}

# D5: reconciliation badge per before→after transition.
_TRANSITION_BADGE = {
    "fixed": (":green[Confirmed by reconciliation ✓]",
              "Fails on the raw OCR text but passes on the parsed values — the "
              "parser's reading satisfies the accounting identity."),
    "still_fail": (":red[Suspect ✗]",
                   "Fails on the raw text AND on the parsed values — likely a real "
                   "source/extraction error, not a parsing artefact."),
    "regressed": (":orange[Parsed values disagree ⚠]",
                  "Passes on the raw text but fails on the parsed values — a parse "
                  "(scale) assumption is probably wrong on this row."),
}


def _eff_passed(check, gi: int) -> bool:
    """Effective pass state, applying any human override. Reads the override widget's
    current state (restored before the run) so the summary metrics never lag a step."""
    seg = st.session_state.get(f"seg_{gi}_{check.check_name}")
    if seg == "Pass":
        return True
    if seg == "Fail":
        return False
    return check.passed  # "Auto" or unset → trust the automatic result


def _check_hint(check):
    """A numeric value from the check that should appear in its target row's cells —
    used to pick the single right row when several share a label. Returns float|None."""
    for attr in ("actual", "expected", "difference"):
        v = getattr(check, attr, None)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _check_targets(check):
    """Cell targets for a check's "Locate", as ``[{"label", "hint", "columns"}]``.

    Prefers the check's precise ``locate`` spec (one entry per relevant row, naming the
    columns whose cells the check uses — a vertical sum down one column, VNC/Var% across
    a few). Falls back to the single ``row_label`` (whole-row outline) for older checks."""
    spec = getattr(check, "locate", None)
    if spec:
        return [dict(e) for e in spec]
    one = getattr(check, "row_label", None)
    if one:
        return [{"label": one, "hint": _check_hint(check), "columns": []}]
    return []


def _highlight_key(grid_df, value_cols) -> str:
    """Grid-key fragment built from the ACTUAL highlight pattern (which cells/rows are
    lit), not just the target identity. st_aggrid caches its render per key, so keying
    on the resolved outcome forces a fresh render whenever the highlight changes — even
    if the same check is re-clicked after the resolver logic changed. Stable across cell
    edits (the focus pattern is unchanged), so edit state is preserved."""
    lit = []
    for ci, c in enumerate(value_cols):
        fc = c + FOC_SUFFIX
        if fc in grid_df.columns:
            lit += [f"{ri}.{ci}" for ri, v in enumerate(grid_df[fc]) if v]
    if FOCUS_COL in grid_df.columns:
        lit += [f"{ri}r" for ri, v in enumerate(grid_df[FOCUS_COL]) if v]
    return ",".join(lit) if lit else "none"


def _edits_key(edited_cells) -> str:
    """Grid-key fragment from the reviewer's committed corrections (D16) — a new
    edit changes the key so the repaint (teal style) is guaranteed to render."""
    if not edited_cells:
        return "e0"
    return "e" + str(len(edited_cells)) + "-" + str(abs(hash(frozenset(edited_cells))) % 99991)


def _user_edited_cells(i: int) -> set:
    """(row, col) cells of table ``i`` the reviewer changed vs the baseline."""
    base = st.session_state.get("baseline_tables") or []
    cur = st.session_state.get("edited_tables") or []
    if i >= len(base) or i >= len(cur):
        return set()
    return {(e["row"], e["col"])
            for e in review_view.diff_tables([base[i]], [cur[i]])}


def _render_check_rows(checks, idx_of, transitions=None):
    """Render a flat list of checks: status · plain message · severity · the
    formal rule it implements (D11) · its before→after reconciliation badge (D5)
    · an explicit, labelled Auto/Pass/Fail override (D10, kept in
    ``check_overrides`` as ``check_{i}_{name}`` → bool, the format
    save_validation reads) · a 'Locate' button that focuses the offending row."""
    transitions = transitions or {}
    for check in checks:
        gi = idx_of[id(check)]
        ovr_key = f"check_{gi}_{check.check_name}"
        stored = st.session_state.check_overrides.get(ovr_key)
        eff = check.passed if stored is None else bool(stored)
        skipped = getattr(check, "skipped", False)
        info = review_view.describe_check(check)

        top = st.columns([0.9, 4, 1.1])
        if skipped:
            top[0].markdown(":gray[**SKIPPED**]")  # D7: a skip is NOT a pass
        else:
            top[0].markdown(":green[**PASS**]" if eff else ":red[**FAIL**]")
        top[1].markdown(f"**{info.title}**")
        # The severity is an ALARM only on a live failure. On PASS/SKIPPED it is
        # just the rule's class — mute it (gray) so a red "CRITICAL" / orange
        # "ERROR" badge never reads as "something is wrong" when nothing failed.
        sev_live = (not skipped) and (not eff)
        top[2].badge(check.severity,
                     color=_SEV_COLOR.get(check.severity, "gray") if sev_live else "gray")
        # Always explain what the rule verifies; tie it to the formal rule (D11).
        st.caption(info.checks)
        if info.rule:
            st.caption(f":gray[{review_view.rule_caption(info.rule)}]")
        # Multi-occurrence checks: counts and stats, not a bare binary verdict.
        stats = getattr(check, "stats", None)
        if isinstance(stats, dict) and stats.get("candidates"):
            bits = [f"{stats.get('passed', 0)}/{stats.get('verified', 0)} pass"]
            if stats.get("failed"):
                bits.append(f":red[{stats['failed']} fail]")
            if stats.get("not_verifiable"):
                bits.append(f":orange[{stats['not_verifiable']} not verifiable]")
            pr = stats.get("pass_rate")
            if isinstance(pr, (int, float)):
                bits.append(f"pass rate {pr:.0%}")
            st.markdown("📊 " + " · ".join(bits))
        # D5: reconciliation badge (before-vs-parsed transition for this check).
        badge = _TRANSITION_BADGE.get(transitions.get(check.check_name))
        if badge:
            st.markdown(badge[0], help=badge[1])
        if skipped:
            st.caption(f":gray[{(check.message or '').strip()}]")
            # A skipped check is still LOCATABLE — coverage entries point at the
            # exact unverifiable cells the reviewer must check manually.
            targets = _check_targets(check)
            if targets:
                n = len(targets)
                help_txt = (f"Highlight '{targets[0]['label']}'" if n == 1
                            else f"Highlight the {n} unverifiable rows") + " in the grid above"
                if st.button("📍 Locate", key=f"loc_{gi}_{check.check_name}", help=help_txt):
                    st.session_state.focus_targets = targets
                    st.rerun()
            st.divider()
            continue  # no override for a check that didn't run
        if not eff:
            detail = (check.message or "").strip()
            st.markdown(
                f"↳ **What to do:** {info.fix}" + (f"  ·  _{detail}_" if detail else "")
            )

        ctrl = st.columns([4, 1])
        with ctrl[0]:
            default = "Pass" if stored is True else ("Fail" if stored is False else "Auto")
            choice = st.segmented_control(
                "Your verdict — overrides the automatic result and is saved",
                ["Auto", "Pass", "Fail"], default=default,
                key=f"seg_{gi}_{check.check_name}", label_visibility="visible",
                help="Auto · trust the automatic check. Pass/Fail · your judgment "
                     "replaces it in the saved report and the quality score.",
            )
            if choice in (None, "Auto"):
                st.session_state.check_overrides.pop(ovr_key, None)
            else:
                st.session_state.check_overrides[ovr_key] = (choice == "Pass")
        with ctrl[1]:
            targets = _check_targets(check)
            if targets:
                st.write("")  # vertical nudge to align with the control
                n = len(targets)
                help_txt = (f"Highlight '{targets[0]['label']}'" if n == 1
                            else f"Highlight the {n} rows this check covers") + " in the grid above"
                if st.button("📍 Locate", key=f"loc_{gi}_{check.check_name}", help=help_txt):
                    st.session_state.focus_targets = targets
                    st.rerun()
        st.divider()


def _tables_fp(tables) -> str:
    """Cheap content fingerprint of the edited tables (D16 auto-mode trigger)."""
    try:
        return "|".join(
            str(pd.util.hash_pandas_object(df.astype(str), index=False).sum())
            for df in tables
        )
    except Exception:
        return str([df.shape for df in tables])


def _cell_basis_info(result, t: int, r: int, col: str) -> dict:
    """Canonical-cell provenance for a grid cell — basis/flag/parsed (D16 trail)."""
    cdict = _canonical_dict(result, t)
    rows = (cdict or {}).get("rows") or []
    if r >= len(rows):
        return {}
    cells = rows[r].get("cells") or {}
    for key, cell in cells.items():
        if review_view._norm(key) == review_view._norm(col):
            return {"basis": cell.get("basis"), "flag": cell.get("flag"),
                    "parsed_value": cell.get("parsed_value")}
    return {}


def _run_reevaluation(result, prev_report, trigger: str):
    """D16: re-validate the CURRENT edited tables, diff against the last snapshot,
    and append CorrectionEvents (every intermediate step is kept — the trail is
    the learning signal: which cells humans fix, from what, to what, and which
    checks that flipped)."""
    from accounting.rule_engine import validate_with_rules
    from accounting.validator import _canonical_numeric_value

    edited = st.session_state.edited_tables
    standard = result.metadata.get('accounting_standard', 'NCT')
    new_report = validate_with_rules(edited, standard=standard)

    prev_snapshot = st.session_state.reeval_snapshot or st.session_state.baseline_tables
    events = review_view.diff_tables(prev_snapshot, edited)
    trans = review_view.pair_reports(prev_report, new_report) if prev_report else {}
    flipped = sorted(n for n, t in trans.items() if t == "fixed")
    regressed = sorted(n for n, t in trans.items() if t == "regressed")

    trail = st.session_state.correction_trail
    step = (max((e.get("step", 0) for e in trail), default=0) + 1)
    score_before = getattr(prev_report, "severity_weighted_score", None) if prev_report else None
    score_after = getattr(new_report, "severity_weighted_score", None)
    ts = datetime.now().isoformat(timespec="seconds")
    for e in events:
        prov = _cell_basis_info(result, e["table"], e["row"], e["col"])
        old_p = _canonical_numeric_value(e["old"])
        new_p = _canonical_numeric_value(e["new"])
        trail.append({
            "step": step, "trigger": trigger, "ts": ts,
            "table": e["table"], "row": e["row"], "col": e["col"],
            "label": e["label"], "old_raw": e["old"], "new_raw": e["new"],
            "parsed_old": old_p, "parsed_new": new_p,
            "cell_basis_before": prov.get("basis"),
            "cell_flag_before": prov.get("flag"),
            "checks_flipped": flipped, "checks_regressed": regressed,
            "score_before": score_before, "score_after": score_after,
        })
    if not events and (flipped or regressed):
        # An override-only or re-run-only flip still deserves a trail entry.
        trail.append({
            "step": step, "trigger": trigger, "ts": ts, "table": None, "row": None,
            "col": None, "label": None, "old_raw": None, "new_raw": None,
            "parsed_old": None, "parsed_new": None, "cell_basis_before": None,
            "cell_flag_before": None, "checks_flipped": flipped,
            "checks_regressed": regressed,
            "score_before": score_before, "score_after": score_after,
        })

    st.session_state.reeval_snapshot = [df.copy() for df in edited]
    st.session_state.validation_report_current = new_report
    st.session_state.reeval_fp = _tables_fp(edited)
    return new_report, flipped, regressed


_LOOP_KIND = {
    "confirmed": (":green[✓ confirmed]", "the existing read satisfies the identity"),
    "resolved": (":green[✓ corrected]", "flipped to the only reading that satisfies the identity"),
    "inferred": (":orange[⚠ inferred]", "missing/garbled cell reconstructed — verify"),
}


def _render_correction_loop(result):
    """The validate → infer → correct LOOP, made visible: per iteration, what
    was inferable, by which rule, with what degree of confidence — and what
    remains ambiguous (not inferable → needs the human)."""
    actions = []
    iters = {}
    remaining = 0
    for i, cdict in _canonical_by_table(result):
        for a in review_view.resolution_entries(cdict):
            actions.append({**a, "table": a.get("table", i)})
        md = (cdict or {}).get("metadata") or {}
        for it in (md.get("resolution_iterations") or []):
            agg = iters.setdefault(it.get("iteration", 1), {"actions": 0, "remaining": 0})
            agg["actions"] += it.get("actions", 0)
            agg["remaining"] += it.get("remaining_ambiguous", 0)
        for rdict in (cdict or {}).get("rows") or []:
            for c in (rdict.get("cells") or {}).values():
                if c.get("basis") in review_view.ASSUMPTION_BASES:
                    remaining += 1
    if not actions and not remaining:
        return
    n_it = max(iters) if iters else (1 if actions else 0)
    title = (f"Correction loop — {len(actions)} correction(s) in {n_it} pass(es)"
             + (f" · {remaining} cell(s) not inferable" if remaining else
                " · nothing further correctable"))
    with st.expander(title, expanded=False):
        st.caption(
            "Validate → infer what's correctable → correct → repeat, until no "
            "rule can settle anything more. A cell corrected in one pass becomes "
            "evidence for the next (corrections cascade). Inferred cells are "
            "never used as evidence."
        )
        for it in sorted(iters):
            st.markdown(f"**Pass {it}** — {iters[it]['actions']} correction(s), "
                        f"{iters[it]['remaining']} ambiguity remaining after")
            for a in [x for x in actions if x.get("iteration", 1) == it]:
                badge, gloss = _LOOP_KIND.get(a.get("kind"), (":gray[·]", ""))
                conf = a.get("confidence")
                conf_txt = f" · confidence {conf:.0%}" if isinstance(conf, (int, float)) else ""
                new_fmt = review_view._fmt_parsed(a.get("new_parsed"))
                st.markdown(
                    f"- {badge} **{a.get('label')}** [{a.get('col')}]: "
                    f"`{a.get('raw') or '∅'}` → **{new_fmt}** — {a.get('rule')}"
                    f"{conf_txt}", help=f"{gloss} · {a.get('note', '')}",
                )
        if remaining:
            st.markdown(
                f":violet[◈] **{remaining} ambiguous cell(s) remain** (indigo) — no "
                "identity could arbitrate them; verify against the source document."
            )


def _grade_of(score) -> tuple[str, str]:
    if score is None:
        return "—", "⚪"
    if score >= 0.9:
        return "excellent", "🟢"
    if score >= 0.75:
        return "good", "🟡"
    if score >= 0.6:
        return "fair", "🟠"
    return "poor", "🔴"


def render_validation_section():
    """The validation section, restructured per D14:
    (1) honest headline (corrected, override-adjusted, skip-excluded score)
    (2) before→after reconciliation summary (D4/D5)
    (3) full rule-coverage panel (D12)
    (4) per-check detail with rule IDs + reconciliation badges (D11/D5)
    (5) dynamic re-evaluation of in-memory corrections (D16)
    (6) labelled override (D10) · (7) honest quality + gated approval (D13)."""
    st.markdown("### ✅ Validation")

    result = st.session_state.extraction_result
    if result is None:
        return

    if 'check_overrides' not in st.session_state:
        st.session_state.check_overrides = {}

    # ---------------------------------------------------------------
    # Auto-correction of variation % (unchanged behaviour)
    # ---------------------------------------------------------------
    try:
        from accounting.validator import validate_extraction, auto_correct_variation_percentages
        from accounting.rule_engine import validate_with_rules
    except ImportError:
        st.warning("Automatic validation module not available")
        return

    tables = st.session_state.edited_tables
    all_corrections = []
    corrected_tables = []
    for i, df in enumerate(tables):
        df_corrected, corrections = auto_correct_variation_percentages(df)
        corrected_tables.append(df_corrected)
        for c in corrections:
            c['table_idx'] = i
            all_corrections.append(c)
    if all_corrections:
        st.session_state.edited_tables = corrected_tables
        tables = corrected_tables
        with st.expander(f"Auto-corrected {len(all_corrections)} variation %", expanded=False):
            for c in all_corrections:
                st.markdown(f"- **{c['label']}**: `{c['original']}` -> `{c['corrected']}`")

    # ---------------------------------------------------------------
    # The validation passes:
    #   RAW    — the original OCR text (result.tables): evidence for the
    #            reconciliation badges ("what did corrections fix?").
    #   FINAL  — the DISPLAYED report: validation runs LIVE against the final
    #            output (working tables = correction loop baked in + your
    #            edits). Edit a cell → next rerun re-validates automatically.
    # ---------------------------------------------------------------
    standard = result.metadata.get('accounting_standard', 'NCT')
    report_raw = None
    try:
        pre_tables = [pd.DataFrame(t) for t in (result.tables or [])]
        report_raw = validate_with_rules(pre_tables, standard=standard)
    except Exception:
        pre_tables = []

    validation_report = None
    try:
        validation_report = validate_with_rules(tables, standard=standard)
    except Exception:
        validation_report = None
    if validation_report is None:
        pipeline_report_dict = result.metadata.get('validation_report')
        if pipeline_report_dict and isinstance(pipeline_report_dict, dict):
            validation_report = _reconstruct_report(pipeline_report_dict)
        else:
            try:
                validation_report = validate_extraction(tables)
            except Exception:
                validation_report = report_raw
    if validation_report is None:
        st.warning("Validation could not run on this document.")
        return

    # Raw → final pairing: which checks the correction loop (+ parsing) fixed.
    transitions = (review_view.pair_reports(report_raw, validation_report)
                   if report_raw is not None else {})
    st.session_state.validation_report = validation_report

    checks = validation_report.checks
    idx_of = {id(c): i for i, c in enumerate(checks)}
    meta, prominent, advisory = review_view.classify_checks(checks)
    eff_by_check = {id(c): _eff_passed(c, idx_of[id(c)]) for c in checks}
    eff_list = [eff_by_check[id(c)] for c in checks]

    # ---------------------------------------------------------------
    # (1) D14 — ONE honest headline. Score is override-adjusted, skip-excluded,
    # meta-free (INFO weight 0); the verdict line is the single source of truth.
    # ---------------------------------------------------------------
    score = review_view.corrected_score(checks, eff_list)
    grade, icon = _grade_of(score)
    acc_fail = [c for c in prominent if not eff_by_check[id(c)] and not c.skipped]
    adv_fail = [c for c in advisory if not eff_by_check[id(c)] and not c.skipped]
    crit_fail = [c for c in acc_fail if c.severity == "CRITICAL"]
    n_skip = sum(1 for c in checks if c.skipped)

    sc = st.columns([1.2, 1, 1, 0.8, 2.4])
    sc[0].metric("Validation score", f"{score:.2f} {icon}" if score is not None else "—",
                 help="Severity-weighted pass rate of the accounting checks — "
                      "override-adjusted; skipped and informational checks excluded.")
    acc_pass = sum(1 for c in prominent if not c.skipped and eff_by_check[id(c)])
    acc_total = sum(1 for c in prominent if not c.skipped)
    adv_pass = sum(1 for c in advisory if not c.skipped and eff_by_check[id(c)])
    adv_total = sum(1 for c in advisory if not c.skipped)
    sc[1].metric("Accounting checks", f"{acc_pass}/{acc_total}",
                 help="The V1/V2 accounting rules — these drive approval.")
    sc[2].metric("Advisory", f"{adv_pass}/{adv_total}",
                 help="Heuristics and ratios (footing, anomalies, V4) — informational.")
    sc[3].metric("Skipped", n_skip, help="Checks that could not run — not counted as passes.")
    with sc[4]:
        if crit_fail:
            st.error(f"⚠ {len(crit_fail)} critical failure(s) — correct in the grid, then Re-evaluate.")
        elif acc_fail:
            st.warning(f"⚠ {len(acc_fail)} accounting check(s) failing — correct in the grid, then Re-evaluate.")
        elif adv_fail:
            st.success(f"✅ All accounting checks pass · {len(adv_fail)} advisory note(s).")
        else:
            st.success("✅ All checks pass.")
        sem = (result.confidence_details or {}).get("semantic_score")
        if isinstance(sem, (int, float)) and score is not None and abs(score - sem) > 0.05:
            st.caption(
                f"Header QCS (semantic {sem:.2f}) was computed at extraction time; "
                f"the live validator score is {score:.2f}. Re-process to refresh the header."
            )

    # ---------------------------------------------------------------
    # (2) D5 — reconciliation summary (raw extraction vs FINAL output).
    # ---------------------------------------------------------------
    if transitions:
        fixed = sorted(n for n, t in transitions.items() if t == "fixed")
        suspect = sorted(n for n, t in transitions.items() if t == "still_fail")
        regressed = sorted(n for n, t in transitions.items() if t == "regressed")
        if fixed or suspect or regressed:
            with st.expander(
                f"Reconciliation — raw extraction vs final output: "
                f"{len(fixed)} fixed ✓ · {len(suspect)} suspect ✗ · {len(regressed)} regressed ⚠",
                expanded=bool(suspect or regressed),
            ):
                st.caption(
                    "The same rules run on the raw OCR text and on the final output "
                    "(parsing + the correction loop + your edits). A failure that "
                    "clears was a reading/scale artefact the corrections fixed; one "
                    "that persists is a real suspect in the source or extraction."
                )
                for n in fixed:
                    st.markdown(f":green[✓] {n} — fixed by parsing/corrections")
                for n in suspect:
                    st.markdown(f":red[✗] {n} — fails on raw AND final values")
                for n in regressed:
                    st.markdown(f":orange[⚠] {n} — passed on raw but fails on the final values")

    # ---------------------------------------------------------------
    # The correction loop — what validation inferred, per iteration, with
    # the degree of confidence of every action, and what remains ambiguous.
    # ---------------------------------------------------------------
    _render_correction_loop(result)

    # ---------------------------------------------------------------
    # (3) D12 — full rule coverage (subsumes "why some checks didn't run").
    # ---------------------------------------------------------------
    segments = st.session_state.get('table_segments', [])
    found_types = {s.statement_type for s in segments} if segments else set()
    cov = review_view.coverage(validation_report, found_types, standard)
    n_rules = sum(b["counts"]["total"] for b in cov.values())
    if cov:
        with st.expander(f"Rule coverage — all {n_rules} rules", expanded=False):
            _ICON = {"pass": "✓", "fail": "✗", "skipped": "⊘", "n/a": "–",
                     "inactive": "∅", "not_run": "·"}
            _CLR = {"pass": "green", "fail": "red", "skipped": "orange",
                    "n/a": "gray", "inactive": "gray", "not_run": "gray"}
            for tier in sorted(cov):
                c = cov[tier]["counts"]
                bits = [f"{c['ran']}/{c['total']} ran"]
                if c.get("pass"):
                    bits.append(f"{c['pass']} pass")
                if c.get("fail"):
                    bits.append(f"{c['fail']} fail")
                if c.get("skipped"):
                    bits.append(f"{c['skipped']} skipped")
                if c.get("n/a"):
                    bits.append(f"{c['n/a']} n/a")
                if c.get("not_run"):
                    bits.append(f"{c['not_run']} not run")
                if c.get("inactive"):
                    bits.append(f"{c['inactive']} inactive")
                st.markdown(f"**{tier}** — " + " · ".join(bits))
                for e in cov[tier]["rules"]:
                    icon = _ICON.get(e["status"], "·")
                    clr = _CLR.get(e["status"], "gray")
                    detail = f" — _{e['detail']}_" if e["detail"] else ""
                    st.markdown(
                        f"&nbsp;&nbsp;:{clr}[{icon}] `{e['rule_id']}` {e['name']}{detail}",
                        unsafe_allow_html=True,
                    )

    # ---------------------------------------------------------------
    # (5) D16 — correction-trail capture. The displayed report is already LIVE
    # against the final output (every rerun re-validates the edited tables);
    # this records the step as a CorrectionEvent batch for learning.
    # ---------------------------------------------------------------
    st.markdown("---")
    rl = st.columns([2.2, 2.2, 3.6])
    reeval_clicked = rl[0].button(
        "📸 Record correction step", use_container_width=True,
        help="Validation already re-runs live on your edits; this snapshots the "
             "step — which cells changed, from what to what, and which checks "
             "flipped — into the correction trail saved with your approval.",
    )
    auto_reeval = rl[1].toggle(
        "Auto-record each edit", key="auto_reeval", value=False,
        help="Append a trail step automatically after every committed cell edit.",
    )
    n_trail = len(st.session_state.correction_trail)
    if n_trail:
        rl[2].caption(f"{n_trail} correction event(s) recorded this session — "
                      "saved with your approval for learning.")
    fp = _tables_fp(st.session_state.edited_tables)
    if reeval_clicked or (auto_reeval and fp != st.session_state.get("reeval_fp")):
        prev = st.session_state.validation_report_current or report_raw
        _, flipped, regressed = _run_reevaluation(
            result, prev, "button" if reeval_clicked else "auto")
        if flipped:
            st.toast(f"✓ {len(flipped)} check(s) fixed by your corrections")
        if regressed:
            st.toast(f"⚠ {len(regressed)} check(s) now failing")
        st.rerun()

    # ---------------------------------------------------------------
    # (4) per-check detail + (6) override + (7) approval.
    # ---------------------------------------------------------------
    col1, col2 = st.columns(2)

    with col1:
        st.markdown("##### Checks")
        st.caption(
            "Automatic accounting checks. Each shows the formal rule it implements "
            "and, when it fails, what to do. The badge on the right is the rule's "
            "**severity** (how much a failure would cost the score) — it lights up "
            "red/orange **only when the check actually fails**; on PASS/SKIPPED it is "
            "muted. **📍 Locate** highlights the offending cells in the grid above."
        )
        if prominent:
            _render_check_rows(review_view.sort_by_severity(prominent), idx_of, transitions)
        else:
            st.caption("No accounting checks produced for this document.")

        if advisory:
            adv_ok = sum(1 for c in advisory if not c.skipped and eff_by_check[id(c)])
            with st.expander(f"Advisory — heuristics & ratios ({adv_ok}/{adv_total})", expanded=False):
                st.caption("Footing / anomaly / ratio heuristics — informational, lower confidence.")
                _render_check_rows(review_view.sort_by_severity(advisory), idx_of, transitions)

        if meta:
            meta_ok = sum(1 for c in meta if not c.skipped and eff_by_check[id(c)])
            with st.expander(f"Structural notes ({meta_ok}/{len(meta)})", expanded=False):
                st.caption("Extraction-structure notes — not accounting rules; never counted "
                           "as accounting passes.")
                _render_check_rows(review_view.sort_by_severity(meta), idx_of, transitions)

        # Manual verification — gates approval (D13: no longer a no-op).
        st.markdown("---")
        st.markdown("#### Manual Verification")
        st.caption("Required before approval — confirms what automation cannot.")
        manual_visual = st.checkbox("Visual accuracy verified", value=False, key="manual_visual",
                                    help="Confirm extracted data matches the source document visually")
        manual_values = st.checkbox("All values match source", value=False, key="manual_values",
                                    help="Confirm all numeric values were extracted correctly")
        manual_rows = st.checkbox("No missing rows", value=False, key="manual_rows",
                                  help="Confirm no table rows were skipped or merged incorrectly")

    with col2:
        st.markdown("#### Quality Assessment")
        st.caption(
            "Rate how well the **automatic** extraction did — judged on the original "
            "automated output, **before your corrections**. This is feedback to "
            "improve the pipeline, so a document you had to fix is *not* Excellent "
            "just because you fixed it."
        )
        # Suggest from the AUTOMATED baseline (pre-human-edit output) + the extraction
        # QCS — NOT the live re-validated report, which already reflects your edits.
        base_tables = st.session_state.get("baseline_tables")
        try:
            base_report = (validate_with_rules(base_tables, standard=standard)
                           if base_tables else validation_report)
        except Exception:
            base_report = validation_report
        default_quality = review_view.suggest_quality(
            base_report.checks,
            [getattr(c, "passed", True) for c in base_report.checks],
            result.qcs_score,
        )
        quality = st.radio(
            "Overall Quality — of the automatic extraction (before your fixes)",
            options=["Excellent", "Good", "Acceptable", "Needs Correction"],
            index=default_quality,
            help="Pre-selected from the AUTOMATED output's checks + QCS (before your "
                 "edits) — change it to your own judgment of how well the automation "
                 "did. ❌ Reject below rejects the document outright.",
        )
        notes = st.text_area("Validation Notes", placeholder="Add any notes about this extraction...")

    # Action buttons
    st.markdown("---")
    st.checkbox(
        "📚 Learn from my label corrections", value=True, key="learn_corrections",
        help="On approval, a corrected OCR label that resolves to a known accounting "
             "term is recorded so the matcher recognises it next time.",
    )
    manual_ok = manual_visual and manual_values and manual_rows
    bcol1, bcol2, bcol3 = st.columns(3)

    with bcol1:
        if st.button("✅ Approve & Save", type="primary", use_container_width=True,
                     disabled=not manual_ok,
                     help=None if manual_ok else
                     "Complete the three Manual Verification confirmations first."):
            learned = save_validation("approved", quality, notes)
            msg = "Extraction approved and saved!"
            if learned:
                msg += f"  📚 Captured {learned} label correction(s) for learning."
            st.success(msg)

    with bcol2:
        if st.button("🔄 Re-process", use_container_width=True):
            if st.session_state.current_file:
                process_document(st.session_state.current_file)
                st.rerun()

    with bcol3:
        if st.button("❌ Reject", use_container_width=True):
            save_validation("rejected", quality, notes)
            st.warning("Extraction rejected.")


def _capture_label_corrections(result, edited_tables, document_id) -> int:
    """Close the HITL → learning loop: when a reviewer fixes a garbled OCR label and
    the corrected text resolves to a known canonical term, record
    ``garbled_raw → canonical`` via the variation logger so the matcher recognises
    that garbled form next time. Best-effort; returns how many were captured."""
    try:
        from accounting.nomenclature import load_default_dictionary
        from accounting.variation_logger import VariationLogger
        nom = load_default_dictionary()
        # Per-tenant learning log (GOV-4). The Streamlit UI has no tenant session yet
        # (per-user login is WP-5a.1), so this resolves to the "default" tenant today.
        vlog = VariationLogger(org_id=st.session_state.get("org_id"))
    except Exception:
        return 0
    orig_tables = getattr(result, "tables", None) or []
    n = 0
    for i, edited in enumerate(edited_tables):
        if i >= len(orig_tables):
            continue
        try:
            orig = pd.DataFrame(orig_tables[i])
        except Exception:
            continue
        lc = review_view._label_col(orig)
        if lc is None or lc not in orig.columns or lc not in edited.columns or len(orig) != len(edited):
            continue
        for r in range(len(orig)):
            old = str(orig[lc].iloc[r]).strip()
            new = str(edited[lc].iloc[r]).strip()
            if not old or not new or old == new:
                continue
            try:
                mr = nom.fuzzy_match(new)
                if mr and mr.entry and mr.match_type in ("exact", "resolved", "fuzzy"):
                    vlog.log_hitl_correction(old, mr.entry.canonical_term, document_id=document_id or "")
                    n += 1
            except Exception:
                continue
    return n


def _build_confirmations(result) -> list:
    """The reviewer's "confirm as-is" actions this session, enriched for persistence
    as a POSITIVE feedback signal (distinct from corrections — nothing changed):
      • ``value``  — a flagged cell whose value the reviewer vouched for.
      • ``mapping``— a weak label→canonical mapping the reviewer vouched for; carries
        the canonical_term so the backend can reinforce that variation.
    """
    confs = []
    tables = st.session_state.get("edited_tables") or []
    for i, cells in (st.session_state.get("confirmed_cells") or {}).items():
        if not (isinstance(i, int) and i < len(tables)):
            continue
        df = tables[i]
        lcol = df.columns[0] if len(df.columns) else None
        for (row, col) in cells:
            if not (isinstance(row, int) and 0 <= row < len(df)):
                continue
            confs.append({
                "kind": "value", "table_index": i, "row_index": row,
                "column_name": col,
                "raw_text": str(df.iloc[row][lcol]) if lcol is not None else "",
                "value": str(df.iloc[row][col]) if col in df.columns else None,
            })
    for i, rows in (st.session_state.get("confirmed_maps") or {}).items():
        if not (isinstance(i, int) and i < len(tables)):
            continue
        df = tables[i]
        lcol = df.columns[0] if len(df.columns) else None
        crows = (_canonical_dict(result, i) or {}).get("rows") or []
        by_label = {}
        for cr in crows:
            by_label.setdefault(review_view._norm(cr.get("raw_text")), cr)
        for row in rows:
            if not (isinstance(row, int) and 0 <= row < len(df)):
                continue
            label = str(df.iloc[row][lcol]) if lcol is not None else ""
            cr = by_label.get(review_view._norm(label))
            confs.append({
                "kind": "mapping", "table_index": i, "row_index": row,
                "column_name": None, "raw_text": label,
                "canonical_term": cr.get("canonical_term") if cr else None,
            })
    return confs


def save_validation(status: str, quality: str, notes: str) -> int:
    """Save the validation result. Returns the number of label corrections captured
    for learning (0 unless approved with learning enabled)."""
    result = st.session_state.extraction_result
    
    # Build the full validation report dict (with user overrides applied)
    saved_report = None
    if st.session_state.get('validation_report'):
        vr = st.session_state.validation_report
        overrides = st.session_state.get('check_overrides', {})
        saved_report = {
            "overall_passed": vr.overall_passed,
            "total_checks": vr.total_checks,
            "passed_checks": vr.passed_checks,
            "failed_checks": vr.failed_checks,
            "critical_failures": vr.critical_failures,
            "severity_weighted_score": (
                round(vr.severity_weighted_score, 3)
                if vr.severity_weighted_score is not None else None
            ),
            "skipped_checks": getattr(vr, "skipped_checks", 0),
            "checks": [
                {
                    "name": c.check_name,
                    "passed": overrides.get(f"check_{i}_{c.check_name}", c.passed),
                    "auto_passed": c.passed,
                    "skipped": getattr(c, "skipped", False),
                    "overridden": f"check_{i}_{c.check_name}" in overrides,
                    "severity": c.severity,
                    "message": c.message,
                    "expected": c.expected,
                    "actual": c.actual,
                    "difference": c.difference,
                    "stats": getattr(c, "stats", None),
                }
                for i, c in enumerate(vr.checks)
            ],
        }

    # Build table segment info
    segment_info = []
    for seg in st.session_state.get('table_segments', []):
        segment_info.append({
            "table_index": seg.table_index,
            "statement_type": seg.statement_type,
            "sub_type": seg.sub_type,
            "confidence": round(seg.confidence, 3),
        })

    validation_record = {
        "timestamp": datetime.now().isoformat(),
        "file": str(st.session_state.current_file) if st.session_state.current_file else None,
        "status": status,
        "quality": quality,
        "notes": notes,
        "qcs_score": result.qcs_score if result else None,
        "tier_used": result.tier_used if result else None,
        "confidence_details": result.confidence_details if result else None,
        "correction_stats": result.metadata.get('correction_stats') if result else None,
        "validation_report": saved_report,
        "table_segments": segment_info,
        "tables": [df.to_dict(orient='records') for df in st.session_state.edited_tables],
        # D16: every intermediate correction step — the learning signal (which
        # cells humans fix, from what to what, and which checks that flipped).
        "correction_trail": list(st.session_state.get("correction_trail") or []),
        # Positive signal: cells/mappings the reviewer vouched for AS-IS (nothing
        # changed). A flagged-but-correct read is as informative as a fix.
        "confirmations": _build_confirmations(result),
        # D13: the manual confirmations are consequential — persist them.
        "manual_verification": {
            "visual_accuracy": bool(st.session_state.get("manual_visual")),
            "values_match_source": bool(st.session_state.get("manual_values")),
            "no_missing_rows": bool(st.session_state.get("manual_rows")),
        },
    }
    
    st.session_state.validation_history.append(validation_record)
    
    # Save to file — a best-effort OFFLINE FALLBACK; the API push below is the
    # source of truth in deployed mode. ./output is the root-owned bind mount in
    # the container, so this can raise PermissionError for the non-root UI uid:
    # degrade to the system temp dir and never let the local copy block the
    # reviewer's submission.
    import tempfile
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"validation_{timestamp}.json"
    for _base in (Path(__file__).parent.parent.parent / "output" / "validations",
                  Path(tempfile.gettempdir()) / "finalze_validations"):
        try:
            _base.mkdir(parents=True, exist_ok=True)
            with open(_base / filename, 'w', encoding='utf-8') as f:
                json.dump(validation_record, f, indent=2, ensure_ascii=False)
            break
        except OSError:
            continue

    # F4: API mode — the dockerized backend is the source of truth. Push the
    # correction trail as Correction rows + move the document through the
    # review state machine; the local JSON above remains the offline fallback.
    import os as _os
    api_url = _os.environ.get("FINALZE_API_URL")
    doc_id = st.session_state.get("api_document_id")
    if api_url and doc_id:
        try:
            with _api_client() as _client:
                corrections = [
                    {
                        "table_index": e.get("table"),
                        "row_index": e.get("row"),
                        "column_name": e.get("col"),
                        "raw_text": e.get("label"),
                        "old_value": e.get("old_raw"),
                        "new_value": e.get("new_raw"),
                        "corrected_canonical": None,
                    }
                    for e in (validation_record.get("correction_trail") or [])
                    if e.get("col") is not None
                ]
                # Confirmations ride the same channel: a value confirm is a
                # no-change correction (old == new, the as-is marker); a mapping
                # confirm carries corrected_canonical = the EXISTING term, which the
                # backend upserts as a reinforced variation candidate.
                for c in (validation_record.get("confirmations") or []):
                    if c["kind"] == "value":
                        corrections.append({
                            "table_index": c["table_index"], "row_index": c["row_index"],
                            "column_name": c["column_name"], "raw_text": c["raw_text"],
                            "old_value": c["value"], "new_value": c["value"],
                            "corrected_canonical": None,
                        })
                    elif c["kind"] == "mapping" and c.get("canonical_term"):
                        corrections.append({
                            "table_index": c["table_index"], "row_index": c["row_index"],
                            "column_name": None, "raw_text": c["raw_text"],
                            "old_value": None, "new_value": None,
                            "corrected_canonical": c["canonical_term"],
                        })
                if corrections:
                    _client.patch_cells(str(doc_id), corrections)
                _client.transition(
                    str(doc_id),
                    "validated" if status == "approved" else "rejected",
                    reason=f"quality={quality}"
                           + (f"; notes={notes[:200]}" if notes else ""),
                )
        except Exception as exc:  # backend down → local JSON already saved
            st.warning(f"Could not persist the review to the backend ({exc}) — "
                       "saved locally instead.")

    # Close the HITL → learning loop on approval (if the reviewer left it enabled).
    learned = 0
    if status == "approved" and st.session_state.get("learn_corrections", True):
        learned = _capture_label_corrections(
            result, st.session_state.edited_tables, validation_record["file"]
        )
    return learned


def render_history_section():
    """Render the validation history section."""
    st.markdown("### 📜 Validation History")
    
    if not st.session_state.validation_history:
        st.info("No validations yet this session.")
        return
    
    for i, record in enumerate(reversed(st.session_state.validation_history)):
        status_icon = "✅" if record["status"] == "approved" else "❌"
        with st.expander(f"{status_icon} {record.get('file', 'Unknown')} - {record['timestamp'][:19]}"):
            st.json(record)


def main():
    """Main application entry point."""
    init_session_state()
    
    # Header
    st.markdown('<p class="main-header">FinAI · Analyse des états financiers</p>', unsafe_allow_html=True)
    st.markdown("Déposez votre document, suivez sa lecture et vérifiez chaque valeur avant validation.")
    
    # Sidebar
    tau_fast, tau_vlm, standard, preprocess_resize, preprocess_grayscale, max_image_width = render_sidebar()
    
    # Main content - simplified to 2 tabs
    tab1, tab2 = st.tabs(["Analyse documentaire", "Historique"])
    
    with tab1:
        # Non-blocking API job tracking: a self-refreshing fragment polls the
        # backend while the rest of the page stays fully interactive.
        if st.session_state.get("api_pending"):
            _api_pending_fragment()

        # Upload section at top
        st.markdown("### 1 · Déposer un document")
        st.caption("Bilan ou compte de résultat · PDF, DOCX, JPG, PNG. Un document hors périmètre est bloqué avec une cause explicite.")
        if st.session_state.get("intake_error"):
            st.error(st.session_state.intake_error)
        
        upload_col1, upload_col2 = st.columns([2, 1])
        
        with upload_col1:
            uploaded_file = st.file_uploader(
                "Déposer un état financier",
                type=["pdf", "docx", "jpg", "jpeg", "png"],
                help="Supported: PDF, DOCX, JPG, PNG",
                label_visibility="collapsed"
            )
            
            # Sample selector — dev convenience only; the deployed UI has no
            # Samples/ mount (SEC-10), so an absent directory simply hides it.
            file_input = None
            sample_files = _list_sample_files()
            if sample_files:
                sample_names = ["-- Or select sample --"] + [f.name for f in sample_files]
                selected_sample = st.selectbox("Samples", sample_names, label_visibility="collapsed")

                if selected_sample != "-- Or select sample --":
                    file_input = _SAMPLES_DIR / selected_sample

            if uploaded_file:
                file_input = uploaded_file
        
        with upload_col2:
            if file_input:
                if st.button("Analyser le document", type="primary", use_container_width=True,
                             disabled=bool(st.session_state.get("api_pending"))):
                    result, elapsed = process_document(
                        file_input,
                        preprocess_resize=preprocess_resize,
                        preprocess_grayscale=preprocess_grayscale,
                        max_image_width=max_image_width,
                        accounting_standard=standard
                    )
                    st.success(f"Analyse terminée en {elapsed:.1f} s · score : {result.qcs_score:.2f} ({standard})")
        
        st.markdown("---")

        # Document-level summary spans the full width FIRST, so the two panels
        # below start at the same vertical level.
        if not st.session_state.get("api_pending") and st.session_state.get("workflow_events"):
            render_progress(st, st.session_state.workflow_events)
        render_extraction_summary()
        if st.session_state.extraction_result:
            st.markdown("---")

        # One selector drives BOTH panels: pick a table → its source page
        # (left) and just that table's grid (right), side by side.
        selected_idx = _render_table_selector()

        # Side-by-side, equal width: Source Document | Editable tables — aligned
        # at the same level (the summary above no longer pushes the grid down).
        source_col, extract_col = st.columns([1, 1])

        with source_col:
            render_source_view(selected_idx)

        with extract_col:
            render_extraction_view(selected_idx)
        
        # Validation section below
        if st.session_state.extraction_result:
            st.markdown("---")
            render_validation_section()
    
    with tab2:
        render_history_section()


if __name__ == "__main__":
    main()
