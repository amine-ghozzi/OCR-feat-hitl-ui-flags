"""Workstream F tests — F3: the worker→API→UI serialization round-trip keeps
canonical tables TYPED (flags, bases, Decimal values), so the flag grid and
the dual validation pass behave identically in-process and through the
dockerized backend. F2: API-mode import hygiene (no heavy ML modules).
"""

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from accounting.canonical_model import CanonicalCell, CanonicalRow, CanonicalTable  # noqa: E402
from accounting.ensemble import canonical_tables_to_parsed_dataframes  # noqa: E402
from hitl import review_view  # noqa: E402

_SRC = Path(__file__).resolve().parents[1] / "src"


def _sample_table() -> CanonicalTable:
    rows = [
        CanonicalRow(
            raw_text="Amortissements", canonical_term=None, match_type="unrecognized",
            section="actifs_non_courant",
            cells={
                "2023": CanonicalCell(raw_value="-13 473.644",
                                      parsed_value=Decimal("-13473644"),
                                      basis="resolved_by_vnc_row", flag="green",
                                      confidence=0.95),
                "2022": CanonicalCell(raw_value="-12 969 942",
                                      parsed_value=Decimal("-12969942"),
                                      basis="plain"),
            },
        ),
        CanonicalRow(
            raw_text="Provisions", match_type="fuzzy", match_confidence=0.81,
            cells={
                "2023": CanonicalCell(raw_value="-216.982",
                                      parsed_value=Decimal("-216982"),
                                      basis="single_sep_thousands"),
                "2022": CanonicalCell(raw_value="", parsed_value=None, flag="yellow"),
            },
        ),
    ]
    ct = CanonicalTable(statement_type="bilan", rows=rows,
                        metadata={"scale_resolutions": [{"rule": "vnc_row"}]})
    ct.recompute_aggregates()
    return ct


def test_roundtrip_preserves_types_and_values():
    ct = _sample_table()
    # to_dict → JSON text (Decimal already stringified) → from_dict
    wire = json.loads(json.dumps(ct.to_dict(), default=str))
    back = CanonicalTable.from_dict(wire)

    assert back.statement_type == "bilan"
    cell = back.rows[0].cells["2023"]
    assert isinstance(cell.parsed_value, Decimal)
    assert cell.parsed_value == Decimal("-13473644")
    assert cell.basis == "resolved_by_vnc_row"
    assert cell.confidence == 0.95
    assert back.rows[1].cells["2022"].parsed_value is None
    assert back.rows[1].cells["2022"].flag == "yellow"
    assert back.rows[1].match_confidence == 0.81
    assert back.metadata["scale_resolutions"][0]["rule"] == "vnc_row"
    # the derived aggregates recompute identically
    assert back.overall_flag == ct.overall_flag
    assert back.match_rate == ct.match_rate


def test_roundtrip_identical_grid_and_parsed_frames():
    ct = _sample_table()
    back = CanonicalTable.from_dict(json.loads(json.dumps(ct.to_dict(), default=str)))

    # D3 parsed frames identical
    a = canonical_tables_to_parsed_dataframes([ct])[0]
    b = canonical_tables_to_parsed_dataframes([back])[0]
    pd.testing.assert_frame_equal(a, b)

    # decorate_for_grid output identical (flags, bases, parsed companion)
    flat = pd.DataFrame([
        {"label": "Amortissements", "2023": "-13 473.644", "2022": "-12 969 942"},
        {"label": "Provisions", "2023": "-216.982", "2022": ""},
    ])
    ga, _, sa = review_view.decorate_for_grid(flat, ct.to_dict())
    gb, _, sb = review_view.decorate_for_grid(flat, back.to_dict())
    pd.testing.assert_frame_equal(ga, gb)
    assert sa == sb
    assert sa["resolved"] == 1 and sa["assumption"] == 1


def test_api_mode_import_stays_light():
    """F2: importing the HITL app modules with FINALZE_API_URL set must not pull
    the heavy ML stack (the thin UI container has none of it installed)."""
    code = (
        "import os, sys\n"
        "os.environ['FINALZE_API_URL'] = 'http://api:8000'\n"
        f"sys.path.insert(0, {str(_SRC)!r})\n"
        "import pipeline\n"
        "from pipeline import ExtractionResult, PipelineConfig\n"
        "from accounting.canonical_model import CanonicalTable\n"
        "from hitl import review_view\n"
        "from client.sdk import FinAlzeClient\n"
        "heavy = sorted({m.split('.')[0] for m in sys.modules if m.split('.')[0] in\n"
        "    ('torch','docling','easyocr','surya','marker','transformers','paddle')})\n"
        "assert not heavy, f'heavy modules imported: {heavy}'\n"
        "print('OK')\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          timeout=180)
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
