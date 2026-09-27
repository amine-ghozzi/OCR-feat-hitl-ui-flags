"""Workstream E tests — E1 row-layout VNC validator + E2 identity-primary
scale resolution (resolve / confirm / infer / conservative no-op / never-force).
"""

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from accounting.canonical_model import CanonicalCell, CanonicalRow, CanonicalTable  # noqa: E402
from accounting.rule_engine import find_vnc_row_triplets, validate_vnc_rows  # noqa: E402
from accounting.scale_resolver import (  # noqa: E402
    resolve_table,
    scale_candidates,
)


# ---------------------------------------------------------------------------
# E1 — row-layout VNC validator
# ---------------------------------------------------------------------------

def _bilan_row_layout_df():
    """Bilan 1.jpg's A2 block: Brut row / Amortissements row / VNC row."""
    return pd.DataFrame({
        "Label": ["Immobilisations corporelles", "Amortissements", "VNC",
                  "Stocks", "Moins provisions pour dépréciation", "VNC"],
        "2023": ["17 210 298", "-13 473.644", "3736654",
                 "11202 126,240", "-1 668 470", "9 533 656"],
        "2022": ["16 759 552", "-12 969 942", "3 789 609",
                 "9 295 099", "-1.603448", "7 691 651"],
    })


def test_find_vnc_row_triplets():
    triplets = find_vnc_row_triplets(_bilan_row_layout_df())
    assert triplets == [(0, 1, 2), (3, 4, 5)]


def test_row_layout_vnc_passes_at_canonical_scale():
    # The column-layout validator never fires here; the row validator must,
    # and the canonical parse (dot=thousands) satisfies every identity.
    results = validate_vnc_rows(_bilan_row_layout_df())
    failures = [r for r in results if not r.passed]
    assert failures == [], [r.message for r in failures]
    assert any(r.check_name == "VNC Row-Layout" and r.passed for r in results)


def test_row_layout_vnc_catches_real_break():
    df = _bilan_row_layout_df()
    df.loc[2, "2023"] = "9 999 999"  # VNC no longer equals Brut − |Amort|
    results = validate_vnc_rows(df)
    row_fails = [r for r in results if not r.passed and r.severity != "INFO"]
    assert len(row_fails) == 1
    assert row_fails[0].locate and len(row_fails[0].locate) == 3  # the 3 cells
    # the family aggregate carries the tally, not a bare binary
    family = [r for r in results if r.check_name == "VNC Row-Layout"]
    assert family and not family[0].passed
    assert family[0].stats["failed"] == 1
    assert family[0].stats["verified"] == family[0].stats["passed"] + 1


# ---------------------------------------------------------------------------
# E2 — candidates
# ---------------------------------------------------------------------------

def test_scale_candidates_both_directions():
    cands = scale_candidates("-13 473.644")
    assert Decimal("-13473644") in cands and Decimal("-13473.644") in cands
    cands2 = scale_candidates("12083312.401")
    assert Decimal("12083312401") in cands2 and Decimal("12083312.401") in cands2


def test_scale_candidates_millimes_collapse():
    # comma-decimal millimes: both readings agree → ONE candidate, no fixed
    # ×1000 ratio assumed.
    assert len(scale_candidates("11202 126,240")) == 1
    # plain integers have a single reading too
    assert len(scale_candidates("17 210 298")) == 1


# ---------------------------------------------------------------------------
# E2 — resolution on canonical tables
# ---------------------------------------------------------------------------

def _cell(raw, parsed, basis="plain"):
    return CanonicalCell(raw_value=raw,
                         parsed_value=Decimal(str(parsed)) if parsed is not None else None,
                         basis=basis)


def _triplet_table(brut, amort, net):
    rows = [
        CanonicalRow(raw_text="Immobilisations corporelles", cells={"2023": brut}),
        CanonicalRow(raw_text="Amortissements", cells={"2023": amort}),
        CanonicalRow(raw_text="VNC", cells={"2023": net}),
    ]
    return CanonicalTable(statement_type="bilan", rows=rows)


def test_resolver_confirms_correct_thousands_assumption():
    # Bilan 1's indigo cell: the canonical thousands read already satisfies VNC.
    ct = _triplet_table(
        _cell("17 210 298", 17210298),
        _cell("-13 473.644", -13473644, basis="single_sep_thousands"),
        _cell("3736654", 3736654),
    )
    out = resolve_table(ct)
    assert len(out) == 1 and out[0].kind == "confirmed" and out[0].rule == "vnc_row"
    cell = ct.rows[1].cells["2023"]
    assert cell.basis == "resolved_by_vnc_row"
    assert cell.parsed_value == Decimal("-13473644")
    assert cell.confidence >= 0.95
    assert ct.metadata["scale_resolutions"][0]["kind"] == "confirmed"


def test_resolver_corrects_wrong_direction():
    # The 12083312.401 case: thousands read (12 083 312 401) breaks the
    # identity; the decimal read satisfies it → resolver flips the value.
    ct = _triplet_table(
        _cell("15 000 000", 15000000),
        _cell("-2 916 688", -2916688),
        _cell("12083312.401", 12083312401, basis="single_sep_thousands"),
    )
    out = resolve_table(ct)
    assert len(out) == 1 and out[0].kind == "resolved"
    cell = ct.rows[2].cells["2023"]
    assert cell.parsed_value == Decimal("12083312.401")
    assert cell.basis == "resolved_by_vnc_row"


def test_resolver_never_forces_balance():
    # NEITHER candidate reconciles (the source itself is inconsistent) →
    # leave the cell indigo, do not fabricate.
    ct = _triplet_table(
        _cell("17 210 298", 17210298),
        _cell("-13 473.644", -13473644, basis="single_sep_thousands"),
        _cell("9 999 999", 9999999),
    )
    out = resolve_table(ct)
    assert out == []
    cell = ct.rows[1].cells["2023"]
    assert cell.basis == "single_sep_thousands"          # untouched
    assert cell.parsed_value == Decimal("-13473644")


def test_resolver_noop_on_ambiguous_neighbours():
    # Two ambiguous cells in one identity → not solvable, leave both.
    ct = _triplet_table(
        _cell("17 210 298", 17210298),
        _cell("-13 473.644", -13473644, basis="single_sep_thousands"),
        _cell("3.736654", 3736654, basis="single_sep_thousands"),
    )
    assert resolve_table(ct) == []
    assert ct.rows[1].cells["2023"].basis == "single_sep_thousands"


def test_resolver_infers_missing_cell_yellow():
    ct = _triplet_table(
        _cell("17 210 298", 17210298),
        _cell("-13 473 644", -13473644),
        _cell("###", None),  # garbled / unparseable
    )
    out = resolve_table(ct)
    assert len(out) == 1 and out[0].kind == "inferred"
    cell = ct.rows[2].cells["2023"]
    assert cell.parsed_value == Decimal("3736654")
    assert cell.basis == "inferred_by_vnc_row"
    assert cell.flag == "yellow"


def test_resolver_loop_cascades_across_passes():
    # The validate → infer → correct LOOP: pass 1's Var% identity settles the
    # ambiguous Brut cell; only then (pass 2) does the VNC identity have two
    # confident contributors and can settle the ambiguous Net cell.
    rows = [
        CanonicalRow(raw_text="Stocks", cells={
            "2023": _cell("15 000.000", 15000000, basis="single_sep_thousands"),
            "2022": _cell("10 000 000", 10000000),
            "%": _cell("+50,0%", None),
        }),
        CanonicalRow(raw_text="Moins provisions pour dépréciation", cells={
            "2023": _cell("-2 916 688", -2916688),
            "2022": _cell("-2 000 000", -2000000),
        }),
        CanonicalRow(raw_text="VNC", cells={
            "2023": _cell("12083312.401", 12083312401, basis="single_sep_thousands"),
            "2022": _cell("8 000 000", 8000000),
        }),
    ]
    ct = CanonicalTable(statement_type="bilan", rows=rows)
    out = resolve_table(ct)
    by = {(o.rule, o.iteration): o for o in out}
    assert ("var_pct", 1) in by and ("vnc_row", 2) in by, [
        (o.rule, o.kind, o.iteration) for o in out]
    # pass 2 flipped the Net cell to the only reading the identity allows
    vnc = by[("vnc_row", 2)]
    assert vnc.kind == "resolved" and vnc.new_parsed == "12083312.401"
    assert ct.rows[2].cells["2023"].parsed_value == Decimal("12083312.401")
    # every action carries a confidence; iteration records are persisted
    assert all(o.confidence > 0 for o in out)
    iters = ct.metadata["resolution_iterations"]
    assert [i["iteration"] for i in iters] == [1, 2]
    assert iters[-1]["remaining_ambiguous"] == 0


def test_resolver_var_pct_confirms_prior_year():
    # The "-1.603448" indigo cell sits in a Var% row: current + reported %
    # are confident → the identity confirms the thousands read.
    row = CanonicalRow(raw_text="Moins provisions pour dépréciation", cells={
        "2023": _cell("-1 668 470", -1668470),
        "2022": _cell("-1.603448", -1603448, basis="single_sep_thousands"),
        "%": _cell("4,1%", None),
    })
    ct = CanonicalTable(statement_type="bilan", rows=[row])
    out = resolve_table(ct)
    assert len(out) == 1 and out[0].rule == "var_pct" and out[0].kind == "confirmed"
    assert row.cells["2022"].basis == "resolved_by_var_pct"
    assert row.cells["2022"].parsed_value == Decimal("-1603448")
