"""Regression (2026-06-15): OCR of an italic/serif capital in a Notes/Réf column
hallucinates an accent — the engine read "A1".."A8" as "À1".."À8" (confirmed in
the raw OCR text, not introduced by any correction). A note/ref code is an ASCII
letter glued to digits, so an accented letter sitting on digits ("À1") is never
real French and must be stripped back — WITHOUT touching genuine accented words.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from preprocessing.text_postprocessor import (  # noqa: E402
    fix_common_ocr_errors, postprocess_table_cell, postprocess_dataframe,
)


def test_phantom_accent_stripped_from_note_codes():
    for bad, good in [("À1", "A1"), ("À8", "A8"), ("(À5)", "(A5)"),
                      ("É2", "E2"), ("Î3", "I3")]:
        assert fix_common_ocr_errors(bad) == good


def test_phantom_accent_stripped_inline():
    assert fix_common_ocr_errors("Immobilisations | À1 | 37430") \
        == "Immobilisations | A1 | 37430"


def test_real_french_accents_are_preserved():
    # The accent only goes when the letter sits DIRECTLY on a digit at a token
    # boundary — real words (incl. accented letters near, but not on, digits)
    # are untouched.
    for s in ["À NOUVEAU", "déjà", "à la clôture", "numéro 3", "clé2",
              "Société 2023", "RÉSULTAT", "Capitaux propres"]:
        assert fix_common_ocr_errors(s) == s


def test_note_column_cleaned_through_dataframe_path():
    # The same fix must reach extracted table cells (postprocess_dataframe is the
    # per-cell cleaner the extractors call on every table).
    df = pd.DataFrame({
        "Designation": ["Immobilisations incorporelles", "Stocks"],
        "Notes": ["À1", "À4"],
        "2023": ["37430", "11202126"],
    })
    postprocess_dataframe(df, "NCT")
    assert list(df["Notes"]) == ["A1", "A4"]


def test_postprocess_table_cell_direct():
    assert postprocess_table_cell("À2", "NCT") == "A2"
    assert postprocess_table_cell("Résultat net", "NCT") == "Résultat net"
