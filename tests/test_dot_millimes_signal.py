"""
WP-2.10 — dot-millimes evidence signal for scale arbitration (VAL-8).

The contract under test is a measurement and an ordering, and nothing else.

VAL-8 observed that "14 267 969.725" parses to 14267969725 — the parser reads
space + dot as a thousands separator. That asymmetry (space + comma is the
decimal form, space + dot is not) is DELIBERATE and pinned by
``tests/test_nomenclature.py``; a blanket dot-decimal rule was tried on this
corpus and reverted at a net loss. So this package does not touch the parser.
It measures how much of a document is typed that way and hands the measurement
to the scale resolver as a second arbitration input beside WP-2.9's unit hint.

Every test here that asserts a value asserts it is *unchanged*.
"""

import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from accounting.ensemble import _CARRY_FORWARD_METADATA  # noqa: E402
from accounting.enrichment_pipeline import enrich_tables  # noqa: E402
from accounting.french_number import (  # noqa: E402
    ASSUMPTION_BASES,
    parse_french_number_with_basis,
)
from accounting.scale_resolver import scale_candidates  # noqa: E402


# --- helpers ---------------------------------------------------------------
def _millimes_frame() -> pd.DataFrame:
    """A frame whose value column is written in the dot-millimes shape, plus one
    cell that is not. Labels are generic — no document content."""
    return pd.DataFrame(
        {
            "Libelle": ["Immobilisations incorporelles", "Stocks", "Clients", "Caisse"],
            "2024": ["14 267 969.725", "1 802 316.400", "-2 095 893.120", "17210"],
        }
    )


def _plain_frame() -> pd.DataFrame:
    """The same shape, typed without the millimes group at all."""
    return pd.DataFrame(
        {
            "Libelle": ["Immobilisations incorporelles", "Stocks", "Clients", "Caisse"],
            "2024": ["14267969", "1802316", "-2095893", "17210"],
        }
    )


def _evidence(cts):
    return [ct.metadata.get("dot_millimes_evidence") for ct in cts]


# --- 1. the measurement ----------------------------------------------------
def test_evidence_share_computed():
    """The share is counted on RAW strings, per table AND per document."""
    cts = enrich_tables([_millimes_frame()], tier=0)
    assert len(cts) == 1
    ev = cts[0].metadata["dot_millimes_evidence"]

    # Three of the four numeric cells are typed "14 267 969.725"; "17210" is not.
    assert ev["cells"] == 3
    assert ev["numeric_cells"] == 4
    assert ev["share"] == 0.75

    # …and the document pass states the same fact at document scope, which is
    # the scope the arbitration hint reads.
    assert ev["document_cells"] == 3
    assert ev["document_numeric_cells"] == 4
    assert ev["document_share"] == 0.75

    # Two tables: the document total is the sum, not one table's opinion.
    both = enrich_tables([_millimes_frame(), _plain_frame()], tier=0)
    per_table = _evidence(both)
    assert [e["cells"] for e in per_table] == [3, 0]
    assert all(e["document_cells"] == 3 for e in per_table)
    assert all(e["document_numeric_cells"] == 8 for e in per_table)
    assert all(e["document_share"] == 0.375 for e in per_table)

    # A document with none of the shape scores a clean zero, never None.
    plain = enrich_tables([_plain_frame()], tier=0)
    assert plain[0].metadata["dot_millimes_evidence"]["cells"] == 0
    assert plain[0].metadata["dot_millimes_evidence"]["share"] == 0.0

    # The measurement is a measurement. A document measured at 75% dot-millimes
    # and one measured at 0% parse their cells the same way: VAL-8's own cell
    # still reads 14267969725, because the parser was not touched.
    assert cts[0].rows[0].cells["value_n"].parsed_value == Decimal("14267969725")
    assert plain[0].rows[0].cells["value_n"].parsed_value == Decimal("14267969")


# --- 2. the whole point: the hint arbitrates, it never rewrites -------------
def test_resolver_receives_hint_and_does_not_rewrite_value():
    """The evidence may ORDER a cell's readings. It may not change one."""
    raw = "14 267 969.725"

    # The parser is untouched: VAL-8's cell still parses the way the ruling says
    # it must, on the basis the ruling names, and that basis is still an
    # assumption — so the cell is still routed to a human.
    parsed, basis = parse_french_number_with_basis(raw)
    assert parsed == Decimal("14267969725")
    assert basis == "single_sep_thousands"
    assert basis in ASSUMPTION_BASES

    plain = scale_candidates(raw)
    assert len(plain) == 2

    # Length-invariant and set-invariant under every hint: nothing is added,
    # nothing is dropped, nothing is rescaled — exactly the unit hint's terms.
    for hint in (None, 0.0, 0.5, 1.0, {"document_share": 0.9}, {"share": 0.9},
                 "nonsense", {}, {"share": None}):
        hinted = scale_candidates(raw, dot_millimes_hint=hint)
        assert sorted(hinted) == sorted(plain), hint

    # A MAJORITY of the document typed this way puts the smaller (naive-decimal)
    # reading first; a minority leaves the canonical order alone.
    assert abs(scale_candidates(raw, dot_millimes_hint=0.9)[0]) < abs(plain[0])
    assert scale_candidates(raw, dot_millimes_hint={"document_share": 0.9})[0] == \
        scale_candidates(raw, dot_millimes_hint=0.9)[0]
    assert scale_candidates(raw, dot_millimes_hint=0.4) == plain
    assert scale_candidates(raw, dot_millimes_hint=0.0) == plain

    # The document share wins over the table's own: one short table is not
    # evidence about how the document was typed.
    assert scale_candidates(
        raw, dot_millimes_hint={"share": 0.0, "document_share": 0.9}
    )[0] == scale_candidates(raw, dot_millimes_hint=0.9)[0]

    # It joins the unit hint rather than fighting it — both order, neither adds.
    assert sorted(scale_candidates(raw, unit_hint="KDT", dot_millimes_hint=0.9)) == \
        sorted(plain)

    # A cell with only one reading stays single-candidate under any evidence.
    assert len(scale_candidates("17 210 298", dot_millimes_hint=1.0)) == 1

    # And the enriched cell itself is untouched by the measurement: same value,
    # same assumption basis. The basis is what keeps it in front of a human —
    # ``review_view`` reads ASSUMPTION_BASES, not the evidence share — so a
    # document measured at 90% dot-millimes still hands this cell to a reviewer
    # rather than quietly restating it.
    ct = enrich_tables([_millimes_frame()], tier=0)[0]
    cell = ct.rows[0].cells["value_n"]
    assert cell.parsed_value == Decimal("14267969725")
    assert cell.basis == "single_sep_thousands" and cell.basis in ASSUMPTION_BASES
    assert ct.metadata["dot_millimes_evidence"]["document_share"] == 0.75


# --- 3. the evidence must survive the Tier-3 vote --------------------------
def test_key_carried_forward():
    """A table fact established before the vote is still true after it.

    The vote reconciles cell VALUES; it does not re-read the raw frame. A key
    missing from ``_CARRY_FORWARD_METADATA`` is silently dropped, and a signal
    the resolver cannot see downstream is no signal at all.
    """
    assert "dot_millimes_evidence" in _CARRY_FORWARD_METADATA
