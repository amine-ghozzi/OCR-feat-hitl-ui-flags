"""Accounting module init.

Lazy (PEP 562): ``from accounting import ValidationResult`` still works, but
merely importing a submodule (e.g. ``accounting.candidate_audit``, which is
stdlib-only) no longer drags in pandas/babel/rapidfuzz — the slim API image
has none of them (F2: only the worker/UI images carry the data stack).
"""

_VALIDATOR_EXPORTS = {
    "ValidationResult",
    "ValidationReport",
    "validate_extraction",
    "validate_balance_sheet",
    "validate_row_sums",
    "validate_variation_percentages",
    "validate_field_types",
    "auto_correct_variation_percentages",
}
_ENSEMBLE_EXPORTS = {
    "EnsembleSource",
    "CellVote",
    "ensemble_vote",
    "consensus_entropy",
    "canonical_tables_to_records",
}

__all__ = sorted(_VALIDATOR_EXPORTS | _ENSEMBLE_EXPORTS)


def __getattr__(name: str):
    if name in _VALIDATOR_EXPORTS:
        from . import validator
        return getattr(validator, name)
    if name in _ENSEMBLE_EXPORTS:
        from . import ensemble
        return getattr(ensemble, name)
    raise AttributeError(f"module 'accounting' has no attribute {name!r}")
