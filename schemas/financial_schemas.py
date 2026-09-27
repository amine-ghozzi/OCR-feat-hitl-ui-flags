"""
Pandera Schemas for Financial Statement Validation

Defines DataFrame schemas for:
- Balance Sheet (Bilan)
- Income Statement (Compte de Résultat)
- Cash Flow Statement (Flux de Trésorerie)

Supports: IFRS, NCT, SYSCOHADA
"""

import pandera as pa
from pandera import Column, Check, DataFrameSchema
from typing import Optional


# =============================================================================
# Balance Sheet Schema (Bilan)
# =============================================================================

balance_sheet_schema = DataFrameSchema(
    columns={
        "account_code": Column(
            str, 
            Check.str_matches(r'^[A-Z]?\d+$'),  # e.g., "A1", "21", "101"
            nullable=True,
            description="Reference note (A1, A2, etc.)"
        ),
        "account_label": Column(
            str,
            Check.str_length(min_value=1),
            description="Account name in French"
        ),
        "amount_current": Column(
            float,
            nullable=True,
            description="Current year amount"
        ),
        "amount_previous": Column(
            float,
            nullable=True,
            description="Previous year amount"
        ),
        "variation": Column(
            float,
            nullable=True,
            description="Absolute variation"
        ),
        "variation_pct": Column(
            float,
            Check.in_range(-1000, 10000),  # Reasonable percentage range
            nullable=True,
            description="Percentage variation"
        ),
    },
    checks=[
        # Coercion failure rate is a QCS metric
    ],
    coerce=True,
    strict=False,  # Allow extra columns
)


# =============================================================================
# Income Statement Schema (Compte de Résultat)
# =============================================================================

income_statement_schema = DataFrameSchema(
    columns={
        "account_code": Column(str, nullable=True),
        "account_label": Column(str, Check.str_length(min_value=1)),
        "amount_current": Column(float, nullable=True),
        "amount_previous": Column(float, nullable=True),
    },
    coerce=True,
    strict=False,
)


# =============================================================================
# Standard-Specific Account Mappings
# =============================================================================

IFRS_ACCOUNTS = {
    "assets": ["Actifs non courants", "Actifs courants", "Total des actifs"],
    "liabilities": ["Passifs non courants", "Passifs courants", "Capitaux propres"],
    "income": ["Chiffre d'affaires", "Produits d'exploitation", "Résultat net"],
}

NCT_ACCOUNTS = {
    "assets": ["Actifs immobilisés", "Actifs circulants", "Total actif"],
    "liabilities": ["Capitaux propres", "Passifs", "Total passif"],
    "income": ["Ventes", "Charges", "Résultat"],
}

SYSCOHADA_ACCOUNTS = {
    "assets": ["Immobilisations", "Actif circulant", "Total actif"],
    "liabilities": ["Ressources stables", "Passif circulant", "Total passif"],
    "income": ["Produits", "Charges", "Résultat net"],
}


def get_schema_for_standard(standard: str, doc_type: str) -> DataFrameSchema:
    """
    Get the appropriate Pandera schema for a given accounting standard.
    
    Args:
        standard: "IFRS", "NCT", or "SYSCOHADA"
        doc_type: "bilan", "compte_resultat", or "flux_tresorerie"
        
    Returns:
        Configured DataFrameSchema
    """
    if doc_type == "bilan":
        return balance_sheet_schema
    elif doc_type == "compte_resultat":
        return income_statement_schema
    else:
        # Default schema
        return balance_sheet_schema


def validate_dataframe(df, standard: str, doc_type: str) -> tuple:
    """
    Validate a DataFrame against the appropriate schema.
    
    Returns:
        (validated_df, coercion_failure_rate, errors)
    """
    schema = get_schema_for_standard(standard, doc_type)
    
    try:
        validated_df = schema.validate(df, lazy=True)
        return validated_df, 0.0, []
    except pa.errors.SchemaErrors as e:
        error_count = len(e.failure_cases)
        total_rows = len(df)
        coercion_failure_rate = error_count / max(total_rows, 1)
        return df, coercion_failure_rate, e.failure_cases.to_dict('records')
