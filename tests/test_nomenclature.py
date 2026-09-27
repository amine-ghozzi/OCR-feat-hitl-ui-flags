"""
Unit tests for Phase 1 Nomenclature foundation.

Covers:
- ``parse_french_number``: 3-layer parser (OCR fixes, locale norm, Babel)
- ``NomenclatureDictionary.resolve_fffd``: FFFD wildcard resolution (§9)
- ``NomenclatureDictionary.fuzzy_match``: 3-stage cascade (§13)
- ``NomenclatureDictionary.classify_statement``: term-distribution classifier (§24)
- ``NomenclatureDictionary.map_to_field``: validation_field bridge
- ``NomenclatureDictionary.get_prompt_vocab``: VLM prompt grounding
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest

from accounting.french_number import is_numeric_token, parse_french_number
from accounting.nomenclature import (
    NomenclatureDictionary,
    _qualifier_set,
    load_default_dictionary,
)


# ===========================================================================
# parse_french_number
# ===========================================================================


class TestFrenchNumber:
    def test_plain_integer(self):
        assert parse_french_number("1500") == Decimal("1500")

    def test_nbsp_thousands(self):
        assert parse_french_number("1\xa0500,00") == Decimal("1500.00")

    def test_regular_space_thousands(self):
        assert parse_french_number("12 450 000") == Decimal("12450000")

    def test_dot_thousands(self):
        assert parse_french_number("1.500,00") == Decimal("1500.00")

    def test_parenthesis_negative(self):
        assert parse_french_number("(1.500,00)") == Decimal("-1500.00")

    def test_dash_is_zero(self):
        assert parse_french_number("-") == Decimal("0")

    def test_em_dash_is_zero(self):
        assert parse_french_number("—") == Decimal("0")

    def test_currency_suffix_stripped(self):
        assert parse_french_number("150,00 DT") == Decimal("150.00")

    def test_currency_prefix_stripped(self):
        assert parse_french_number("DT 1.234,56") == Decimal("1234.56")

    def test_ocr_zero_letter(self):
        assert parse_french_number("l5O,00") == Decimal("150.00")

    def test_simple_decimal_comma(self):
        assert parse_french_number("1,5") == Decimal("1.5")

    def test_empty_string(self):
        assert parse_french_number("") is None

    def test_garbage(self):
        assert parse_french_number("abc") is None

    def test_none(self):
        assert parse_french_number(None) is None

    def test_negative_sign(self):
        assert parse_french_number("-1.500,00") == Decimal("-1500.00")

    def test_int_passthrough(self):
        assert parse_french_number(42) == Decimal("42")

    def test_float_passthrough(self):
        assert parse_french_number(1.5) == Decimal("1.5")

    def test_is_numeric_token_accepts_valid(self):
        assert is_numeric_token("1 500,00")
        assert is_numeric_token("(1.234)")
        assert is_numeric_token("-")

    def test_is_numeric_token_rejects_text(self):
        assert not is_numeric_token("Immobilisations")
        assert not is_numeric_token("")

    # --- ISSUE-1 (A): single separator + 3-digit group = THOUSANDS ----------
    # A '.'/',' followed by exactly a 3-digit group (no real fraction) is a
    # thousands separator in these integer-amount NCT/French statements.
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("10 070.594", Decimal("10070594")),
            ("-37.430", Decimal("-37430")),
            ("-13 473.644", Decimal("-13473644")),
            ("-216.982", Decimal("-216982")),
            ("-290.279", Decimal("-290279")),
            ("210,580", Decimal("210580")),
            ("-166,074", Decimal("-166074")),
            ("-118,594", Decimal("-118594")),
            # >3 trailing digits: treated as thousands for this integer corpus.
            ("-1.603448", Decimal("-1603448")),
        ],
    )
    def test_single_separator_is_thousands(self, raw, expected):
        assert parse_french_number(raw) == expected

    def test_single_separator_1_2_digits_stays_decimal(self):
        # Guard: the disambiguation must NOT swallow genuine 1-2 digit decimals.
        assert parse_french_number("1,5") == Decimal("1.5")
        assert parse_french_number("1.5") == Decimal("1.5")
        assert parse_french_number("12,34") == Decimal("12.34")
        assert parse_french_number("99.99") == Decimal("99.99")

    # --- ISSUE-1 (B): angle-bracket negatives -------------------------------
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("<950>", Decimal("-950")),
            ("<7788327>", Decimal("-7788327")),
            ("<2095 893>", Decimal("-2095893")),
        ],
    )
    def test_angle_bracket_negative(self, raw, expected):
        assert parse_french_number(raw) == expected

    # --- ISSUE-1 (C): trailing minus → leading sign -------------------------
    def test_trailing_minus_negative(self):
        assert parse_french_number("060201-") == Decimal("-60201")

    # --- A1: space-grouped thousands + comma = DECIMAL ----------------------
    # A space already groups thousands, so a following comma is the decimal
    # separator — while a following DOT stays thousands, and a comma WITHOUT a
    # space group stays thousands (both per ISSUE-1). All three must coexist.
    @pytest.mark.parametrize("raw, expected", [
        ("11202 126,240", Decimal("11202126.240")),  # comma after space group = decimal
        ("37 158,492", Decimal("37158.492")),
        ("10 070.594", Decimal("10070594")),          # dot after space group = thousands (ISSUE-1)
        ("210,580", Decimal("210580")),               # comma, no space = thousands (ISSUE-1)
        ("1,5", Decimal("1.5")),                       # short decimal unchanged
    ])
    def test_space_group_comma_is_decimal(self, raw, expected):
        assert parse_french_number(raw) == expected

    # --- B3: confidence honesty — risky recoveries flagged ------------------
    @pytest.mark.parametrize("raw, risky", [
        ("060201-", True),        # trailing-minus sign recovery
        ("-52955 0", True),       # stray digit-split
        ("11202 126,240", False), # clean decimal, not risky
        ("210,580", False),
        ("-166,074", False),
        ("<2095 893>", False),    # valid 3-digit thousands group
        ("10 070.594", False),
    ])
    def test_numeric_parse_is_risky(self, raw, risky):
        from accounting.french_number import numeric_parse_is_risky
        assert numeric_parse_is_risky(raw) is risky

    # --- Per-document comma-convention inference (completes A1) --------------
    @pytest.mark.parametrize("tokens, expected", [
        # decimal signal: space-grouped thousands alongside a comma
        (["2 000 000,000", "3811603,553", "1005000,000"], "comma_decimal"),
        # decimal signal: comma with only 1-2 trailing digits
        (["1,5", "313653,853"], "comma_decimal"),
        # thousands signal: multiple commas in one token
        (["1,234,567", "210,580"], "comma_thousands"),
        # no unambiguous signal → unknown (caller falls back to default)
        (["210,580", "166,074"], "unknown"),
        ([], "unknown"),
    ])
    def test_infer_comma_convention(self, tokens, expected):
        from accounting.french_number import infer_comma_convention
        assert infer_comma_convention(tokens) == expected

    def test_comma_decimal_hint_resolves_no_space_comma(self):
        # The case A1 alone could not fix: a no-space comma-decimal in a
        # comma-decimal document resolves correctly with the hint.
        assert parse_french_number("3811603,553", comma_decimal=True) == Decimal("3811603.553")
        assert parse_french_number("1005000,000", comma_decimal=True) == Decimal("1005000.000")

    def test_comma_decimal_hint_is_per_document(self):
        # Same token resolves per the document's convention.
        assert parse_french_number("210,580", comma_decimal=True) == Decimal("210.580")
        assert parse_french_number("210,580", comma_decimal=False) == Decimal("210580")

    @pytest.mark.parametrize("raw, expected", [
        ("34,340,000", Decimal("34340000")),   # was None — million-scale EF_CLB values
        ("10,590,695", Decimal("10590695")),
        ("7,840,000", Decimal("7840000")),
        ("1,234,567", Decimal("1234567")),
        ("210,580", Decimal("210580")),         # single comma unchanged
    ])
    def test_multi_comma_thousands(self, raw, expected):
        # 2+ commas with no dot = thousands grouping (a number has at most one
        # decimal point), regardless of document convention.
        assert parse_french_number(raw) == expected

    def test_comma_decimal_default_is_backward_compatible(self):
        # No hint (None) preserves the exact prior token-level behaviour.
        assert parse_french_number("210,580") == Decimal("210580")
        assert parse_french_number("10 070.594") == Decimal("10070594")
        assert parse_french_number("11202 126,240") == Decimal("11202126.240")
        assert parse_french_number("1,5") == Decimal("1.5")


# ===========================================================================
# NomenclatureDictionary — loading & indices
# ===========================================================================


@pytest.fixture(scope="module")
def nom() -> NomenclatureDictionary:
    load_default_dictionary.cache_clear()
    return load_default_dictionary()


class TestDictionaryLoading:
    def test_loads_entries(self, nom):
        assert len(nom.entries) >= 50

    def test_covers_all_three_statements(self, nom):
        stmts = set(e.statement_type for e in nom.entries)
        assert stmts == {"bilan", "compte_resultat", "flux_tresorerie"}

    def test_column_models_registered(self, nom):
        assert "brut_amort_net" in nom.column_models
        assert "n_n1" in nom.column_models

    def test_sections_registered(self, nom):
        assert "actifs_non_courants" in nom.sections
        assert "capitaux_propres" in nom.sections
        assert "flux_exploitation" in nom.sections

    def test_every_entry_has_normalized(self, nom):
        assert all(e.normalized for e in nom.entries)

    def test_every_entry_has_statement_type(self, nom):
        for e in nom.entries:
            assert e.statement_type in {"bilan", "compte_resultat", "flux_tresorerie"}


# ===========================================================================
# resolve_fffd (§9)
# ===========================================================================


class TestFFFDResolution:
    def test_single_wildcard_unambiguous(self, nom):
        resolved, conf, n = nom.resolve_fffd("R�sultat de l'exercice")
        assert resolved == "Résultat de l'exercice"
        assert conf >= 0.99
        assert n == 1

    def test_two_wildcards(self, nom):
        resolved, conf, _ = nom.resolve_fffd("Immobilisations financi�res")
        assert resolved == "Immobilisations financières"
        assert conf >= 0.80

    def test_multiple_wildcards_same_word(self, nom):
        resolved, conf, _ = nom.resolve_fffd("Charges financi�res")
        assert resolved == "Charges financières"
        assert conf >= 0.80

    def test_resolves_via_variation(self, nom):
        resolved, conf, _ = nom.resolve_fffd("Tr�sorerie de cl�ture")
        assert resolved == "Trésorerie en fin d'exercice"
        assert conf >= 0.80

    def test_question_mark_wildcard(self, nom):
        resolved, conf, _ = nom.resolve_fffd("R?sultat de l'exercice")
        assert resolved == "Résultat de l'exercice"
        assert conf >= 0.99

    def test_no_wildcards_returns_identity(self, nom):
        text = "Produits d'exploitation"
        resolved, conf, n = nom.resolve_fffd(text)
        assert resolved == text
        assert conf == 1.0
        assert n == 0

    def test_empty_input(self, nom):
        resolved, conf, n = nom.resolve_fffd("")
        assert resolved == ""
        assert n == 0


# ===========================================================================
# fuzzy_match (§13)
# ===========================================================================


class TestFuzzyMatch:
    def test_exact_match(self, nom):
        mr = nom.fuzzy_match("Capital social")
        assert mr.match_type == "exact"
        assert mr.confidence == 1.0
        assert mr.entry.validation_field == "capital_social"

    def test_accent_insensitive_exact(self, nom):
        mr = nom.fuzzy_match("capital social")
        assert mr.match_type == "exact"

    def test_variation_match(self, nom):
        mr = nom.fuzzy_match("chiffre d'affaires")
        assert mr.match_type in ("exact", "resolved")
        assert mr.entry is not None
        assert mr.entry.canonical_term == "Revenus"

    def test_fuzzy_abbreviation(self, nom):
        mr = nom.fuzzy_match("Immobilisat. incorpor.")
        assert mr.entry is not None
        assert mr.entry.canonical_term == "Immobilisations incorporelles"

    def test_section_scoping_narrows_candidates(self, nom):
        mr = nom.fuzzy_match("autres actifs non courants", section="actifs_non_courants")
        assert mr.entry is not None
        assert mr.entry.section == "actifs_non_courants"

    def test_unknown_term_is_custom(self, nom):
        mr = nom.fuzzy_match("Contribution sociale de solidarité")
        assert mr.match_type in ("custom", "unrecognized")
        assert mr.entry is None

    def test_empty_input(self, nom):
        mr = nom.fuzzy_match("")
        assert mr.match_type == "unrecognized"
        assert mr.entry is None


# ===========================================================================
# Discriminative-qualifier guard (residual/total exclusivity)
# ===========================================================================


class TestQualifierSet:
    """`_qualifier_set` detects meaning-flipping tokens in clean and OCR-mangled forms."""

    def test_bare_concept_has_no_qualifier(self):
        assert _qualifier_set("produits d exploitation") == frozenset()

    def test_whole_token_total_and_autres(self):
        assert _qualifier_set("total des produits d exploitation") == frozenset({"total"})
        assert _qualifier_set("autres produits d exploitation") == frozenset({"autres"})

    def test_ocr_concatenated_prefix_detected(self):
        # Single concatenated token: "total" is a prefix, not a whole token.
        assert _qualifier_set("totalactifsimmobilises") == frozenset({"total"})
        assert _qualifier_set("actifsimmobilises") == frozenset()

    def test_net_and_brut_families(self):
        assert _qualifier_set("resultat net") == frozenset({"net"})
        assert _qualifier_set("valeur brute") == frozenset({"brut"})

    # --- ISSUE-4: section-discriminating tokens split the `total` pool ---------
    def test_section_qualifiers_split_total_pool(self):
        # Bare grand-total vs section-qualified siblings land in different pools.
        assert _qualifier_set("total des passifs") == frozenset({"total"})
        assert _qualifier_set("total des passifs non courants") == frozenset(
            {"total", "non_courants"}
        )
        assert _qualifier_set("total des passifs courants") == frozenset(
            {"total", "courants"}
        )
        # "propres" is intentionally NOT a discriminator (see _SECTION_QUALIFIERS):
        # an equity total carries only {"total"}, so a bare "capitaux et passifs"
        # grand-total is not falsely flagged against the "capitaux propres" form.
        assert _qualifier_set("total des capitaux propres") == frozenset({"total"})

    def test_non_courant_does_not_also_tag_courant(self):
        # "non courants" must win — a non-current label is never tagged {"courants"}.
        qs = _qualifier_set("total des passifs non courants")
        assert "non_courants" in qs
        assert "courants" not in qs


class TestQualifierGuard:
    """A bare header must not fuzzy-match its ``Autres …``/``Total des …`` sibling,
    while genuine total/residual lines and OCR-mangled totals still match."""

    def test_header_does_not_match_residual_autres(self, nom):
        # "PRODUITS D'EXPLOITATION" used to mismatch "Autres produits d'exploitation".
        mr = nom.fuzzy_match("PRODUITS D'EXPLOITATION")
        assert mr.entry is None or "autres" not in mr.entry.canonical_term.lower()

    def test_header_does_not_match_total(self, nom):
        # "CAPITAUX PROPRES ET PASSIFS" (no qualifier) must not grab the "Total des …" line.
        mr = nom.fuzzy_match("CAPITAUX PROPRES ET PASSIFS")
        assert mr.entry is None or not mr.entry.canonical_term.lower().startswith("total")

    def test_real_total_line_still_matches(self, nom):
        mr = nom.fuzzy_match("TOTAL DES CAPITAUX PROPRES ET PASSIFS")
        assert mr.entry is not None
        assert mr.entry.canonical_term == "Total des capitaux propres et des passifs"

    def test_ocr_concatenated_total_still_matches(self, nom):
        mr = nom.fuzzy_match("TotalActifsImmobilises")
        assert mr.entry is not None
        assert mr.entry.canonical_term == "Total des actifs immobilisés"

    def test_residual_on_both_sides_still_matches(self, nom):
        # Both query and target carry "autres" → guard allows the match.
        mr = nom.fuzzy_match("Placement & autres actifs financiers")
        assert mr.entry is not None
        assert mr.entry.canonical_term == "Placements et autres actifs financiers"

    # --- ISSUE-4: grand-totals must not collapse onto a qualified sibling ------
    def test_grand_total_passifs_resolves_to_itself(self, nom):
        # "TOTAL DES PASSIFS" must map to the grand-total, NOT "... non courants".
        mr = nom.fuzzy_match("TOTAL DES PASSIFS")
        assert mr.entry is not None
        assert mr.entry.canonical_term == "Total des passifs"
        assert mr.entry.validation_field == "total_passif"

    def test_grand_total_capitaux_et_passifs_resolves_to_grand_total(self, nom):
        # Must map to the equity+liabilities grand-total, not "Total des capitaux propres".
        mr = nom.fuzzy_match("TOTAL DES CAPITAUX PROPRES ET PASSIFS")
        assert mr.entry is not None
        assert mr.entry.canonical_term == "Total des capitaux propres et des passifs"

    def test_total_passifs_non_courants_still_self_maps(self, nom):
        mr = nom.fuzzy_match("Total des passifs non courants")
        assert mr.entry is not None
        assert mr.entry.canonical_term == "Total des passifs non courants"

    def test_pre_affectation_equity_total_maps_to_equity_total(self, nom):
        # ISSUE-4: newly added variation — pre-affectation equity total.
        mr = nom.fuzzy_match("TOTAL DES CAPITAUX AVANT AFFECTATION")
        assert mr.entry is not None
        assert mr.entry.canonical_term == "Total des capitaux propres"

    def test_grand_total_passifs_does_not_collapse_to_non_courants(self, nom):
        # The collapse class ISSUE-4 guards against, expressed at the matcher level.
        mr = nom.fuzzy_match("TOTAL DES PASSIFS")
        assert mr.entry is None or "courant" not in mr.entry.canonical_term.lower()


# ===========================================================================
# Mapping correctness — ISSUE-2 / ISSUE-3 / ISSUE-5 regression pins
# ===========================================================================


class TestMappingCorrectness:
    """Pins for the cross-statement leak fix and the new YAML synonyms."""

    # --- ISSUE-3: "production stockée" collision resolved ----------------

    def test_production_stockee_binds_to_its_own_canonical(self, nom):
        """The collided variation must bind to ``Production stockée``, not
        ``Autres produits d'exploitation`` (first-registration-wins fix)."""
        mr = nom.fuzzy_match("production stockée")
        assert mr.entry is not None
        assert mr.entry.canonical_term == "Production stockée"

    def test_production_stockee_variation_finished_goods(self, nom):
        mr = nom.fuzzy_match("Variation des stocks des produits finis et des encours")
        assert mr.entry is not None
        assert mr.entry.canonical_term == "Production stockée"

    def test_autres_produits_no_longer_owns_production_stockee(self, nom):
        entry = nom.lookup("Autres produits d'exploitation")
        assert entry is not None
        assert "production stockee" not in entry.normalized_variations()

    # --- ISSUE-3: missing CR synonyms ------------------------------------

    def test_charges_financieres_nettes_maps_via_curated_variation(self, nom):
        """``Charges financières nette(s)`` resolves to ``Charges financières``
        via a CURATED variation (Stage-2 lookup), not a fuzzy collapse. The net
        financial-charges line canonicalizes to financial charges, consistent
        with the reference across Etat_résultat 1/2/3. This is a deliberate,
        reviewed mapping — distinct from the fuzzy {net}-vs-bare collapse the
        qualifier guard still blocks (a non-curated 'net' label won't match)."""
        for label in ("Charges financières nette", "Charges financières nettes"):
            mr = nom.fuzzy_match(label, statement_type="compte_resultat")
            assert mr.entry is not None
            assert mr.entry.canonical_term == "Charges financières"
            assert mr.match_type in ("resolved", "exact")

    def test_impot_sur_les_benefices_singular(self, nom):
        mr = nom.fuzzy_match("Impot sur les benefices")
        assert mr.entry is not None
        assert mr.entry.canonical_term == "Impôts sur les bénéfices"

    # --- ISSUE-5: Réserves detail synonyms -------------------------------

    def test_reserve_speciale_reevaluation_maps_to_reserves(self, nom):
        mr = nom.fuzzy_match("Réserve spéciale de réévaluation")
        assert mr.entry is not None
        assert mr.entry.canonical_term == "Réserves"

    def test_reserves_pour_investissements_maps_to_reserves(self, nom):
        mr = nom.fuzzy_match("Réserves pour investissements")
        assert mr.entry is not None
        assert mr.entry.canonical_term == "Réserves"

    # --- ISSUE-2: cross-statement leak guard -----------------------------

    def test_cr_row_does_not_match_bilan_provisions(self, nom):
        """A compte_resultat label must not leak into bilan ``Provisions``
        when the matcher is scoped to compte_resultat."""
        mr = nom.fuzzy_match("Dotations aux provisions", statement_type="compte_resultat")
        if mr.entry is not None:
            assert mr.entry.statement_type == "compte_resultat"
            assert mr.entry.canonical_term != "Provisions"

    def test_cr_row_does_not_match_bilan_stocks(self, nom):
        mr = nom.fuzzy_match("Variation des stocks", statement_type="compte_resultat")
        if mr.entry is not None:
            assert mr.entry.statement_type == "compte_resultat"
            assert mr.entry.canonical_term != "Stocks"

    def test_statement_scope_narrows_to_statement(self, nom):
        mr = nom.fuzzy_match("Revenus", statement_type="compte_resultat")
        assert mr.entry is not None
        assert mr.entry.statement_type == "compte_resultat"

    def test_section_and_statement_intersect_blocks_stale_header(self, nom):
        """A stale bilan section header on a CR row must not re-widen scope
        back into bilan vocabulary when statement_type is compte_resultat."""
        mr = nom.fuzzy_match(
            "Stocks", section="actifs_courants", statement_type="compte_resultat"
        )
        if mr.entry is not None:
            assert mr.entry.statement_type == "compte_resultat"

    def test_none_statement_preserves_full_scope(self, nom):
        """Backward-compat: statement_type=None keeps prior behaviour."""
        mr = nom.fuzzy_match("Capital social", statement_type=None)
        assert mr.match_type == "exact"
        assert mr.entry.canonical_term == "Capital social"


# ===========================================================================
# classify_statement (§24)
# ===========================================================================


class TestStatementClassification:
    def test_classifies_bilan(self, nom):
        rows = [
            "Immobilisations corporelles",
            "Stocks",
            "Clients et comptes rattachés",
            "Capital social",
        ]
        assert nom.classify_statement(rows) == "bilan"

    def test_classifies_compte_resultat(self, nom):
        rows = [
            "Revenus",
            "Charges de personnel",
            "Résultat financier",
            "Résultat d'exploitation",
        ]
        assert nom.classify_statement(rows) == "compte_resultat"

    def test_classifies_flux_tresorerie(self, nom):
        rows = [
            "Flux net d'exploitation",
            "Augmentation de capital",
            "Dividendes versés",
            "Trésorerie en fin d'exercice",
        ]
        assert nom.classify_statement(rows) == "flux_tresorerie"

    def test_empty_returns_unknown(self, nom):
        assert nom.classify_statement([]) == "unknown"

    def test_brut_amort_net_booster_favors_bilan(self, nom):
        rows = ["Immobilisations corporelles", "Brut", "Amortissement", "Net"]
        assert nom.classify_statement(rows) == "bilan"


# ===========================================================================
# map_to_field
# ===========================================================================


class TestMapToField:
    def test_canonical_term(self, nom):
        assert nom.map_to_field("Capital social") == "capital_social"

    def test_via_variation(self, nom):
        assert nom.map_to_field("chiffre d'affaires") == "chiffre_affaires"

    def test_accent_insensitive(self, nom):
        assert nom.map_to_field("resultat de l exercice") == "resultat_exercice"

    def test_unknown_returns_none(self, nom):
        assert nom.map_to_field("Totally Unknown Term") is None


# ===========================================================================
# get_prompt_vocab
# ===========================================================================


class TestPromptVocab:
    def test_produces_non_empty_vocab_per_statement(self, nom):
        for stmt in ("bilan", "compte_resultat", "flux_tresorerie"):
            vocab = nom.get_prompt_vocab(stmt)
            assert vocab.strip()
            assert all(line.startswith("- ") for line in vocab.splitlines() if line)

    def test_bilan_vocab_includes_core_items(self, nom):
        vocab = nom.get_prompt_vocab("bilan")
        assert "Capital social" in vocab
        assert "Stocks" in vocab
        assert "Total des actifs" in vocab

    def test_cr_vocab_includes_core_items(self, nom):
        vocab = nom.get_prompt_vocab("compte_resultat")
        assert "Revenus" in vocab
        assert "Charges de personnel" in vocab
        assert "Résultat d'exploitation" in vocab

    def test_tft_vocab_includes_core_items(self, nom):
        vocab = nom.get_prompt_vocab("flux_tresorerie")
        assert "Dividendes versés" in vocab
        assert "Variation nette de trésorerie" in vocab


# ===========================================================================
# classify_section
# ===========================================================================


class TestSectionClassifier:
    def test_actifs_non_courants(self, nom):
        assert nom.classify_section("ACTIFS NON COURANTS") == "actifs_non_courants"

    def test_capitaux_propres(self, nom):
        assert nom.classify_section("Capitaux propres") == "capitaux_propres"

    def test_passifs_courants(self, nom):
        assert nom.classify_section("PASSIFS COURANTS") == "passifs_courants"

    def test_unknown_header_returns_none(self, nom):
        assert nom.classify_section("This is a random sentence with no header keyword") is None


class TestTailDefectFixes:
    """The 5 long-tail defects exposed by the RUN6 panel (all deterministic)."""

    def test_dot_in_comma_decimal_doc_is_a_known_ambiguity(self):
        # Defect 3 was ATTEMPTED then REVERTED: a lone dot in a comma-decimal
        # document is genuinely ambiguous — sometimes a comma OCR'd as '.'
        # (decimal, "-1 098 948.081") and sometimes a real thousands grouper
        # ("1.603448"), with no per-token signal to separate them. A blanket rule
        # regressed more cells than it fixed, so ISSUE-1's thousands reading stays.
        assert parse_french_number("10 070.594") == Decimal("10070594")
        # the comma path (A1) is unaffected and still works
        assert parse_french_number("11202 126,240") == Decimal("11202126.240")

    def test_grand_total_resolves_under_capitaux_section_scope(self, nom):
        # Defect 1/2: compound capitaux+passifs grand total must not collapse onto
        # the equity-only total even when the row is scoped to capitaux_propres.
        mr = nom.fuzzy_match("TOTAL DES CAPITAUX PROPRES ET PASSIFS", section="capitaux_propres")
        assert mr.entry is not None
        assert mr.entry.canonical_term == "Total des capitaux propres et des passifs"

    def test_intermediate_equity_subtotal_left_unmapped(self, nom):
        # Defect 4: "...avant résultat de l'exercice" is intermediate → null;
        # "...avant affectation" is the final total → maps.
        mr = nom.fuzzy_match("Total capitaux propres avant resultat de l'exercice", section="capitaux_propres")
        assert mr.entry is None
        mr2 = nom.fuzzy_match("Total des capitaux propres avant affectation", section="capitaux_propres")
        assert mr2.entry is not None and mr2.entry.canonical_term == "Total des capitaux propres"

    def test_truncated_label_matches_variation_alias(self, nom):
        # Defect 5: OCR-truncated label resolves via the variation-alias fallback.
        mr = nom.fuzzy_match("Variation des stocks des produits finis et des enc", statement_type="compte_resultat")
        assert mr.entry is not None and mr.entry.canonical_term == "Production stockée"

    def test_alias_fallback_does_not_override_canonical(self, nom):
        # The fallback must not perturb an existing canonical match.
        mr = nom.fuzzy_match("TotalActifsImmobilises")
        assert mr.entry is not None and mr.entry.canonical_term == "Total des actifs immobilisés"


class TestParseBasis:
    """parse_french_number_with_basis reports HOW the value was derived (Part A)."""

    def test_basis_values(self):
        from accounting.french_number import parse_french_number_with_basis as PB
        assert PB("210,580") == (Decimal("210580"), "single_sep_thousands")
        assert PB("34,340,000") == (Decimal("34340000"), "multi_comma")
        assert PB("11202 126,240") == (Decimal("11202126.240"), "space_comma_decimal")
        assert PB("3811603,553", comma_decimal=True) == (Decimal("3811603.553"), "comma_decimal_hint")
        assert PB("1500") == (Decimal("1500"), "plain")
        assert PB("1,5") == (Decimal("1.5"), "plain")
        assert PB("<2095 893>") == (Decimal("-2095893"), "angle_negative")
        assert PB("060201-") == (Decimal("-60201"), "trailing_minus")
        assert PB("abc") == (None, None)

    def test_assumption_bases_membership(self):
        from accounting.french_number import ASSUMPTION_BASES
        assert "single_sep_thousands" in ASSUMPTION_BASES
        assert "comma_decimal_hint" in ASSUMPTION_BASES
        assert "multi_comma" not in ASSUMPTION_BASES      # deterministic
        assert "space_comma_decimal" not in ASSUMPTION_BASES
        assert "plain" not in ASSUMPTION_BASES

    def test_parse_unchanged_by_refactor(self):
        # the public scalar fn must equal the with-basis value for every input
        from accounting.french_number import parse_french_number as P, parse_french_number_with_basis as PB
        for raw in ["1500", "1,5", "210,580", "34,340,000", "-1.500,00", "(1.234)", "—", "", "abc"]:
            assert P(raw) == PB(raw)[0]


# ===========================================================================
# OCR-13 fallout — a preserved newline must not blind the structural boosters
# ===========================================================================


class TestBoosterWhitespace:
    """``classify_statement_detailed``'s keyword scan is whitespace-insensitive.

    Every structural keyword is multi-word, and since OCR-13 the extracted text
    keeps the whitespace it was read with — a cell that wraps mid-label reaches
    the scan as ``"actifs\\nnon courants"``. The scan collapses runs the way the
    matching keys already did, so the booster still fires.
    """

    def test_multiword_booster_survives_internal_newline(self):
        d = load_default_dictionary()

        flat = d.classify_statement_detailed(["Actifs non courants", "Total"])
        wrapped = d.classify_statement_detailed(["Actifs\nnon courants", "Total"])

        assert wrapped.booster_scores["bilan"] >= 3
        assert flat.booster_scores == wrapped.booster_scores
        assert flat.statement_type == wrapped.statement_type

    def test_every_whitespace_run_collapses(self):
        d = load_default_dictionary()

        for spacing in ("\n", "\r\n", "\t", "  ", "\xa0", " ", " \n "):
            rows = [f"Produits{spacing}d'exploitation", "Total"]
            cls = d.classify_statement_detailed(rows)
            assert cls.booster_scores["compte_resultat"] >= 3, repr(spacing)
