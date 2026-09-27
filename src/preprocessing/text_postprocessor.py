"""
French Financial Text Post-Processor

Fixes common OCR issues in French financial documents:
- Missing spaces between concatenated words (ACTIFSNONCOURANTS → ACTIFS NON COURANTS)
- Numeric formatting issues
- Common OCR character substitutions

Terms are loaded from YAML config files in the terms/ directory.
Supports: IFRS, NCT (Tunisia), SYSCOHADA (OHADA zone)
"""

import re
import logging
import unicodedata
from pathlib import Path
from typing import List, Tuple, Dict, Optional

logger = logging.getLogger(__name__)

# =============================================================================
# YAML Term Loader
# =============================================================================

TERMS_DIR = Path(__file__).parent / "terms"

# Cache for loaded terms
_terms_cache: Dict[str, List[Tuple[str, str]]] = {}


def load_terms_from_yaml(standard: str) -> List[Tuple[str, str]]:
    """
    Load term corrections from YAML config file.
    
    Args:
        standard: Accounting standard code (IFRS, NCT, SYSCOHADA)
        
    Returns:
        List of (pattern, correction) tuples
    """
    # Check cache first
    if standard in _terms_cache:
        return _terms_cache[standard]
    
    yaml_path = TERMS_DIR / f"{standard.lower()}.yaml"
    
    if not yaml_path.exists():
        logger.warning(f"Terms file not found: {yaml_path}")
        return []
    
    try:
        import yaml
        
        with open(yaml_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        
        terms = []
        
        # Extract terms from all sections (skip metadata)
        for section, items in data.items():
            if section == 'metadata' or not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and 'pattern' in item and 'correction' in item:
                    terms.append((item['pattern'], item['correction']))
        
        # Cache the result
        _terms_cache[standard] = terms
        logger.info(f"Loaded {len(terms)} terms for {standard}")
        
        return terms
        
    except ImportError:
        logger.warning("PyYAML not installed, using fallback hardcoded terms")
        return _get_fallback_terms(standard)
    except Exception as e:
        logger.error(f"Error loading terms for {standard}: {e}")
        return _get_fallback_terms(standard)


def _get_fallback_terms(standard: str) -> List[Tuple[str, str]]:
    """Fallback hardcoded terms if YAML loading fails."""
    # Minimal fallback terms
    common = [
        ("TOTALACTIF", "TOTAL ACTIF"),
        ("TOTALPASSIF", "TOTAL PASSIF"),
        ("CAPITAUXPROPRES", "CAPITAUX PROPRES"),
        ("RESULTATNET", "RÉSULTAT NET"),
        ("CHIFFREDAFFAIRES", "CHIFFRE D'AFFAIRES"),
    ]
    return common


def get_terms(standard: str = "NCT") -> List[Tuple[str, str]]:
    """
    Get term corrections for the specified accounting standard.
    
    Args:
        standard: One of 'IFRS', 'NCT', 'SYSCOHADA'
        
    Returns:
        List of (pattern, correction) tuples
    """
    return load_terms_from_yaml(standard)


def clear_terms_cache():
    """Clear the terms cache to force reload from YAML files."""
    global _terms_cache
    _terms_cache = {}
    logger.info("Terms cache cleared")


# =============================================================================
# Pattern Matching
# =============================================================================

# Patterns for detecting camelCase-like concatenation in French
CAMELCASE_PATTERN = re.compile(r'([a-zéèêëàâäùûüîïôöç])([A-ZÉÈÊËÀÂÄÙÛÜÎÏÔÖÇ])')

# Pattern for detecting lowercase followed by uppercase
MISSING_SPACE_PATTERN = re.compile(r'([a-zéèêëàâäùûüîïôöç]{2,})([A-ZÉÈÊËÀÂÄÙÛÜÎÏÔÖÇ][a-zéèêëàâäùûüîïôöç]+)')


# =============================================================================
# Post-Processing Functions
# =============================================================================

def fix_missing_spaces(text: str, accounting_standard: str = "NCT") -> str:
    """
    Fix missing spaces and misspellings in OCR output.

    Uses smart correction (word-break DP + fuzzy matching) when available,
    falls back to dictionary lookup otherwise.

    Args:
        text: Raw OCR text
        accounting_standard: One of 'IFRS', 'NCT', 'SYSCOHADA'

    Returns:
        Text with proper spacing and corrected words
    """
    if not text:
        return text

    # Try smart corrector first (word-break + fuzzy)
    try:
        from preprocessing.smart_corrector import build_vocabulary, smart_correct
        vocab = build_vocabulary(accounting_standard)
        return smart_correct(text, vocab)
    except ImportError:
        pass

    # Fallback: original dictionary approach
    result = text
    terms = get_terms(accounting_standard)

    for wrong, correct in terms:
        pattern = re.compile(re.escape(wrong), re.IGNORECASE)
        result = pattern.sub(correct, result)

    result = CAMELCASE_PATTERN.sub(r'\1 \2', result)
    result = MISSING_SPACE_PATTERN.sub(r'\1 \2', result)

    return result


def fix_numeric_formatting(text: str) -> str:
    """
    Fix numeric formatting issues.
    
    Args:
        text: Raw OCR text
        
    Returns:
        Text with corrected numeric formatting
    """
    if not text:
        return text
    
    result = text
    
    # =============================================================
    # Fix underline-caused errors (line below text read as part of char)
    # =============================================================
    
    # "=944" should be "-944" (underline makes hyphen look like equals)
    result = re.sub(r'^=(\d)', r'-\1', result)  # Start of string
    result = re.sub(r'\s=(\d)', r' -\1', result)  # After whitespace
    
    # ":944" should be "-944" (underline misread as colon)
    result = re.sub(r'^:(\d)', r'-\1', result)  # Start of string
    result = re.sub(r'\s:(\d)', r' -\1', result)  # After whitespace
    
    # "_944" should be "-944" (underline read as underscore)
    result = re.sub(r'^_(\d)', r'-\1', result)
    result = re.sub(r'\s_(\d)', r' -\1', result)
    
    # "~944" should be "-944" (tilde misread)
    result = re.sub(r'^~(\d)', r'-\1', result)
    result = re.sub(r'\s~(\d)', r' -\1', result)
    
    # =============================================================
    # Fix common OCR character substitutions in numbers
    # =============================================================
    
    result = re.sub(r'(\d)O(\d)', r'\g<1>0\2', result)  # O → 0
    result = re.sub(r'(\d)o(\d)', r'\g<1>0\2', result)  # o → 0
    result = re.sub(r'(\d)l(\d)', r'\g<1>1\2', result)  # l → 1
    result = re.sub(r'(\d)I(\d)', r'\g<1>1\2', result)  # I → 1
    result = re.sub(r'(\d)S(\d)', r'\g<1>5\2', result)  # S → 5
    result = re.sub(r'(\d)B(\d)', r'\g<1>8\2', result)  # B → 8
    
    return result


def fffd_resolve_text(
    text: str, accounting_standard: str = "NCT"
) -> Tuple[str, int]:
    """Replace U+FFFD / "?" placeholders with canonical Nomenclature terms.

    Walks each line, and for any line that contains a wildcard, asks the
    :class:`NomenclatureDictionary` to produce the best candidate using
    its wildcard-search algorithm (§9). Lines with no wildcard are
    returned untouched.

    Only NCT is wired right now — IFRS/SYSCOHADA fall through to identity
    until Phase 6 adds multi-standard dictionaries.

    Returns ``(resolved_text, resolutions_applied)``. The count is used by
    the QCS penalty layer alongside other correction statistics.
    """
    if not text:
        return text, 0
    if accounting_standard.upper() != "NCT":
        return text, 0
    if not any(ch in text for ch in ("�", "?")):
        return text, 0

    try:
        from accounting.nomenclature import load_default_dictionary
    except ImportError:
        logger.warning("Nomenclature dictionary not available — skipping FFFD resolution")
        return text, 0

    dictionary = load_default_dictionary()
    resolved_lines: List[str] = []
    resolutions = 0

    for line in text.splitlines():
        if not any(ch in line for ch in ("�", "?")):
            resolved_lines.append(line)
            continue
        # Heuristic: only try phrase-level resolution if the line is short
        # enough to be a label (≤ 120 chars). Longer lines are likely
        # narrative text where wildcards belong to unrelated words.
        if len(line) > 120:
            resolved_lines.append(line)
            continue
        resolved, confidence, _ = dictionary.resolve_fffd(line)
        if resolved != line and confidence >= 0.80:
            resolved_lines.append(resolved)
            resolutions += 1
        else:
            resolved_lines.append(line)

    return "\n".join(resolved_lines), resolutions


# A note/reference code is an ASCII letter glued to digits (A1, B2, A8). OCR of
# an italic/serif capital sometimes hallucinates an accent (A→À, E→É), and an
# accented letter sitting directly on top of digits ("À1") is never real French
# — strip it back to its base letter. Anchored at a token boundary with the
# accented letter immediately before the digit, so genuine accented words
# ("À NOUVEAU", "déjà", "numéro") are never touched.
_PHANTOM_CODE_ACCENT_RE = re.compile(
    r"\b([ÀÁÂÃÄÅÈÉÊËÌÍÎÏÒÓÔÕÖÙÚÛÜÝàáâãäåèéêëìíîïòóôõöùúûüý])(?=\d)"
)


def _strip_phantom_code_accent(text: str) -> str:
    """Drop a phantom OCR accent from a note/ref code letter glued to digits."""
    return _PHANTOM_CODE_ACCENT_RE.sub(
        lambda m: unicodedata.normalize("NFKD", m.group(1))[0], text
    )


def fix_common_ocr_errors(text: str) -> str:
    """
    Fix common OCR character substitution errors.

    Args:
        text: Raw OCR text

    Returns:
        Text with corrected characters
    """
    if not text:
        return text

    result = text

    # Common French character fixes
    replacements = [
        ("'", "'"),  # Smart quote to apostrophe
        (""", '"'),
        (""", '"'),
        ("–", "-"),  # En-dash to hyphen
        ("—", "-"),  # Em-dash to hyphen
        ("…", "..."),
    ]

    for old, new in replacements:
        result = result.replace(old, new)

    # Note/ref code letters wrongly accented by OCR (e.g. "À1" → "A1").
    result = _strip_phantom_code_accent(result)

    return result


def postprocess_text(text: str, accounting_standard: str = "NCT") -> str:
    """
    Apply all post-processing fixes to OCR text.
    
    Args:
        text: Raw OCR text
        accounting_standard: One of 'IFRS', 'NCT', 'SYSCOHADA'
        
    Returns:
        Cleaned and corrected text
    """
    if not text:
        return text
    
    result = text
    
    # Apply fixes in order
    result = fix_missing_spaces(result, accounting_standard)
    result = fix_numeric_formatting(result)
    result = fix_common_ocr_errors(result)
    
    return result


def postprocess_text_with_stats(text: str, accounting_standard: str = "NCT") -> Tuple[str, Dict[str, int]]:
    """
    Apply all post-processing fixes and track correction statistics.
    
    For QCS penalty calculation - more corrections = lower raw quality.
    
    Args:
        text: Raw OCR text
        accounting_standard: One of 'IFRS', 'NCT', 'SYSCOHADA'
        
    Returns:
        Tuple of (cleaned text, correction stats dict)
    """
    if not text:
        return text, {"total_corrections": 0}
    
    stats = {
        "term_corrections": 0,
        "camelcase_fixes": 0,
        "missing_space_fixes": 0,
        "underline_fixes": 0,
        "ocr_char_fixes": 0,
        "fffd_resolutions": 0,
        "total_corrections": 0,
    }

    # 0. FFFD resolution against Nomenclature — runs first so downstream
    # term-dictionary passes see canonical accented forms.
    resolved, fffd_count = fffd_resolve_text(text, accounting_standard)
    stats["fffd_resolutions"] = fffd_count
    result = resolved

    # 1. Smart correction (word-break + fuzzy) or fallback dictionary
    try:
        from preprocessing.smart_corrector import (
            build_vocabulary,
            smart_correct,
            split_ignoring_placeholders,
        )
        vocab = build_vocabulary(accounting_standard)
        corrected = smart_correct(result, vocab)
        if corrected != result:
            # `<!-- … -->` placeholders are machine tokens that smart_correct
            # leaves untouched; they must not be counted as corrections, and
            # dropping them from both sides keeps the word sequences aligned
            # (OCR-10).
            before = split_ignoring_placeholders(result)
            after = split_ignoring_placeholders(corrected)
            # Count changed words as term corrections
            stats["term_corrections"] = sum(1 for a, b in zip(before, after) if a != b)
            # Account for word count changes (concatenation splits)
            stats["missing_space_fixes"] = max(0, len(after) - len(before))
            result = corrected
    except ImportError:
        # Fallback: original dictionary approach
        terms = get_terms(accounting_standard)
        for wrong, correct in terms:
            pattern = re.compile(re.escape(wrong), re.IGNORECASE)
            matches = len(pattern.findall(result))
            if matches > 0:
                result = pattern.sub(correct, result)
                stats["term_corrections"] += matches

        matches = len(CAMELCASE_PATTERN.findall(result))
        result = CAMELCASE_PATTERN.sub(r'\1 \2', result)
        stats["camelcase_fixes"] = matches

        matches = len(MISSING_SPACE_PATTERN.findall(result))
        result = MISSING_SPACE_PATTERN.sub(r'\1 \2', result)
        stats["missing_space_fixes"] = matches
    
    # 4. Fix underline-caused errors (count before fixing)
    underline_patterns = [
        (r'^=(\d)', r'-\1'),
        (r'\s=(\d)', r' -\1'),
        (r'^:(\d)', r'-\1'),
        (r'\s:(\d)', r' -\1'),
        (r'^_(\d)', r'-\1'),
        (r'\s_(\d)', r' -\1'),
    ]
    for pattern_str, replacement in underline_patterns:
        matches = len(re.findall(pattern_str, result, re.MULTILINE))
        result = re.sub(pattern_str, replacement, result, flags=re.MULTILINE)
        stats["underline_fixes"] += matches
    
    # 5. Fix OCR character substitutions
    ocr_patterns = [
        (r'(\d)O(\d)', r'\g<1>0\2'),
        (r'(\d)o(\d)', r'\g<1>0\2'),
        (r'(\d)l(\d)', r'\g<1>1\2'),
        (r'(\d)I(\d)', r'\g<1>1\2'),
        (r'(\d)S(\d)', r'\g<1>5\2'),
        (r'(\d)B(\d)', r'\g<1>8\2'),
    ]
    for pattern_str, replacement in ocr_patterns:
        matches = len(re.findall(pattern_str, result))
        result = re.sub(pattern_str, replacement, result)
        stats["ocr_char_fixes"] += matches
    
    # 6. Common character replacements
    result = fix_common_ocr_errors(result)
    
    # Total corrections
    stats["total_corrections"] = (
        stats["term_corrections"] +
        stats["camelcase_fixes"] +
        stats["missing_space_fixes"] +
        stats["underline_fixes"] +
        stats["ocr_char_fixes"] +
        stats["fffd_resolutions"]
    )
    
    return result, stats


def postprocess_table_cell(value: str, accounting_standard: str = "NCT") -> str:
    """
    Post-process a single table cell value.
    
    Args:
        value: Cell value as string
        accounting_standard: One of 'IFRS', 'NCT', 'SYSCOHADA'
        
    Returns:
        Cleaned cell value
    """
    if not isinstance(value, str):
        return value
    
    return postprocess_text(value.strip(), accounting_standard)


def postprocess_dataframe(df, accounting_standard: str = "NCT") -> None:
    """
    Post-process all string columns in a DataFrame (in-place).
    
    Args:
        df: pandas DataFrame to process
        accounting_standard: One of 'IFRS', 'NCT', 'SYSCOHADA'
    """
    # Positional iteration: a docling-exported table can carry DUPLICATE column
    # names, where df[col] returns a DataFrame (no .dtype) and the whole table
    # export is dropped ("'DataFrame' object has no attribute 'dtype'").
    for i in range(df.shape[1]):
        series = df.iloc[:, i]
        if series.dtype == object:  # String columns
            df.iloc[:, i] = series.apply(
                lambda x: postprocess_table_cell(x, accounting_standard) if isinstance(x, str) else x
            )


# =============================================================================
# Exports for backward compatibility
# =============================================================================

# These can be accessed if needed but prefer using get_terms()
STANDARD_TERMS = {
    "IFRS": lambda: get_terms("IFRS"),
    "NCT": lambda: get_terms("NCT"),
    "SYSCOHADA": lambda: get_terms("SYSCOHADA"),
}


if __name__ == "__main__":
    # Test loading and post-processing
    print("Testing YAML-based term loading...")
    
    for standard in ["IFRS", "NCT", "SYSCOHADA"]:
        terms = get_terms(standard)
        print(f"\n{standard}: {len(terms)} terms loaded")
        if terms:
            print(f"  Sample: {terms[0]}")
    
    print("\nPost-processing tests:")
    test_cases = [
        "ACTIFSNONCOURANTS",
        "CHIFFREDAFFAIRES",
        "RESULTATNET",
    ]
    
    for test in test_cases:
        result = postprocess_text(test, "NCT")
        print(f"  {test!r:40} → {result!r}")
