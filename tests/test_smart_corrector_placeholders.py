"""
OCR-10 — Docling placeholder whitelist in ``smart_correct``.
OCR-13 — newline preservation in ``smart_correct``.
OCR-15 — post-processed text is independent of ``PYTHONHASHSEED``.

Docling's ``export_to_markdown()`` writes HTML comments as structural
placeholders (``<!-- image -->`` for every picture, ``<!-- page_break -->``).
They are machine tokens, not OCR output. Before the whitelist the word ``image``
fuzzy-matched ``MARGE`` at distance 2, so every picture-bearing document got
``<!-- image -->`` rewritten to ``<!-- marge -->`` and was charged a QCS
correction penalty for text nobody read off a page.

Covers:
- the placeholder survives ``smart_correct`` byte-for-byte
- the placeholder contributes nothing to ``postprocess_text_with_stats`` counts
- a genuine typo next to a placeholder is still corrected and still counted
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from preprocessing.smart_corrector import (  # noqa: E402
    build_vocabulary,
    is_placeholder,
    smart_correct,
    split_ignoring_placeholders,
)
from preprocessing.text_postprocessor import postprocess_text_with_stats  # noqa: E402


DOCLING_MARKDOWN = "Bilan\n\n<!-- image -->\n\n| Actifs | 2024 |"


def test_image_placeholder_preserved():
    """`<!-- image -->` passes smart_correct untouched — no `marge` rewrite."""
    vocab = build_vocabulary("NCT")

    out = smart_correct(DOCLING_MARKDOWN, vocab)

    assert "<!-- image -->" in out
    assert "marge" not in out.lower()
    # Other Docling placeholders are opaque too, whole-string or inline.
    assert smart_correct("<!-- page_break -->", vocab) == "<!-- page_break -->"
    assert smart_correct("<!--image-->", vocab) == "<!--image-->"

    # Helper contract the statistics path relies on.
    assert is_placeholder("<!-- image -->") is True
    assert is_placeholder("<!--") is False
    assert split_ignoring_placeholders(DOCLING_MARKDOWN) == [
        "Bilan", "|", "Actifs", "|", "2024", "|",
    ]


def test_placeholder_not_counted_as_correction():
    """A placeholder must not inflate the counts that feed the QCS penalty."""
    _, stats = postprocess_text_with_stats(DOCLING_MARKDOWN, "NCT")

    assert stats["term_corrections"] == 0
    assert stats["missing_space_fixes"] == 0
    assert stats["total_corrections"] == 0


def test_real_typo_still_corrected():
    """The whitelist is narrow: OCR typos beside a placeholder still correct."""
    vocab = build_vocabulary("NCT")

    assert smart_correct("Amortissments", vocab) == "Amortissements"

    text, stats = postprocess_text_with_stats("Amortissments\n\n<!-- image -->\n", "NCT")

    assert "Amortissements" in text
    assert "<!-- image -->" in text
    # Exactly one correction: the typo, never the placeholder.
    assert stats["term_corrections"] == 1
    assert stats["total_corrections"] == 1


# =============================================================================
# OCR-13 — the whitespace between words survives correction
# =============================================================================


def test_newlines_preserved():
    """`smart_correct` re-joins with the ORIGINAL separators, not single spaces.

    It runs over the whole markdown document, so collapsing every whitespace run
    flattened `ExtractionResult.text` to one line for both tiers (OCR-13).
    """
    vocab = build_vocabulary("NCT")

    out = smart_correct(DOCLING_MARKDOWN, vocab)

    assert out == "Bilan\n\n<!-- image -->\n\n| Actifs | 2024 |"
    assert out.count("\n") == DOCLING_MARKDOWN.count("\n")

    # Every whitespace run is carried over byte for byte — tabs and CRLF too.
    assert smart_correct("Bilan\r\n\tActifs", vocab) == "Bilan\r\n\tActifs"
    # Correcting a word does not disturb the separators around it.
    assert smart_correct("Amortissments\n\nProvsions", vocab) == (
        "Amortissements\n\nProvisions"
    )
    # Leading/trailing whitespace is still dropped, as it always was.
    assert smart_correct("  Bilan  ", vocab) == "Bilan"

    # ...and the statistics path still sees exactly the tokens it always saw.
    assert split_ignoring_placeholders(DOCLING_MARKDOWN) == [
        "Bilan", "|", "Actifs", "|", "2024", "|",
    ]


def test_markdown_table_rows_stay_on_own_lines():
    """A Docling markdown table keeps one row per line through the corrector."""
    vocab = build_vocabulary("NCT")

    table = (
        "| Libellé | 2024 |\n"
        "|---|---|\n"
        "| TOTALACTIF | 1 000 |\n"
        "| CAPITAUXPROPRES | 2 000 |\n"
    )

    out = smart_correct(table, vocab)
    lines = out.split("\n")

    assert len(lines) == 4                     # the trailing newline is stripped
    assert lines[1] == "|---|---|"             # the separator row is untouched
    assert all(line.startswith("|") and line.endswith("|") for line in lines)
    # The word-break correction still fires inside its own cell.
    assert "TOTAL ACTIF" in lines[2]
    assert "1 000" in lines[2]
    # A row never bleeds into the next one.
    assert "2 000" not in lines[2]


# =============================================================================
# OCR-15 — the post-processed text does not ride Python's randomised hash
# =============================================================================

# Words chosen to land on fuzzy ties: several vocabulary entries sit at the same
# edit distance, so `fuzzy_match` picks whichever `Vocabulary.by_length` happens
# to list first. Before the fix that list was ordered by string hash.
_SEED_PROBE_TEXT = (
    "TOTALACTIFSNONCOURANTS amortissments provsions exploitaton "
    "immoblisations creances clents frs divers charg produt"
)

_SEED_PROBE_CHILD = """
import hashlib, sys
sys.path.insert(0, sys.argv[1])
from preprocessing.text_postprocessor import postprocess_text_with_stats
out, _ = postprocess_text_with_stats(sys.argv[2], "NCT")
sys.stdout.write(hashlib.sha256(out.encode("utf-8")).hexdigest())
"""


def _postprocess_in_subprocess(seed: str, tmp_path: Path) -> str:
    """sha256 of the post-processed probe text, run under `PYTHONHASHSEED=seed`."""
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = seed
    # The learning log is per-tenant and must never be a tracked file.
    env["FINALZE_VARIATION_LOG"] = str(tmp_path / f"variation_{seed}.jsonl")
    proc = subprocess.run(
        [sys.executable, "-c", _SEED_PROBE_CHILD, str(ROOT / "src"), _SEED_PROBE_TEXT],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, f"seed {seed} child failed:\n{proc.stderr}"
    return proc.stdout.strip()


def test_text_is_hash_seed_independent(tmp_path):
    """Two processes with different hash seeds post-process to the same bytes.

    `ExtractionResult.text` had the same char count and QCS but a different
    `text_sha` between runs, because `Vocabulary` iterated the vocabulary SET:
    `by_length` — which `fuzzy_match` scans first-best-wins — came out in string
    hash order (OCR-15). `golden_sweep.py` pins the seed; nothing else does.
    """
    first = _postprocess_in_subprocess("1", tmp_path)
    second = _postprocess_in_subprocess("2", tmp_path)

    assert first == second, "post-processed text still depends on PYTHONHASHSEED"

    # And it is the same text this process (whatever seed it was started with)
    # produces, so the in-process and subprocess paths cannot drift apart.
    in_process, _ = postprocess_text_with_stats(_SEED_PROBE_TEXT, "NCT")
    assert hashlib.sha256(in_process.encode("utf-8")).hexdigest() == first
