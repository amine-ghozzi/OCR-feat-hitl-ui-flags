"""Conservative admission gate. No accounting correction or numeric normalization."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from zipfile import BadZipFile
from datetime import datetime, timezone
from pathlib import Path


class DocumentRejected(ValueError):
    def __init__(self, report):
        self.report = report
        super().__init__(f"{report['code']} — {report['reason']} {report['action']}")


def event(stage, state, message, **details):
    return dict(stage=stage, state=state, message=message,
                at=datetime.now(timezone.utc).isoformat(), **details)


def snapshot(text, tables, engine):
    """Detached engine output, including original columns; hash detects later changes."""
    payload = json.loads(json.dumps({
        "engine": engine, "text": text,
        "tables": [{"columns": list(t.columns), "index": list(t.index),
                    "data": t.to_numpy().tolist()} for t in tables],
    }, ensure_ascii=False, default=str))
    payload["sha256"] = hashlib.sha256(json.dumps(
        payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return payload


def classify(text, *, complete=True):
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    number_count = len(re.findall(r"\b\d[\d .,]*\d\b", t))
    groups = {
        "bilan": (r"\bbilan\b|balance sheet|statement of financial position",
                  ["actif", "passif", "capitaux propres", "immobilisations", "assets", "liabilities", "equity"]),
        "compte_resultat": (r"(?:etat|compte).{0,12}resultat|income statement|profit and loss",
                            ["charges", "produits", "resultat", "revenus", "chiffre d'affaires", "revenue", "expenses", "profit"]),
    }
    found = [kind for kind, (title, terms) in groups.items()
             if re.search(title, t) and sum(term in t for term in terms) >= 2 and number_count >= 3]
    if found:
        return dict(accepted=True, code="admitted", types=found,
                    reason="Indices d’un bilan ou d’un compte de résultat détectés.", action="")
    ambiguous = (not complete or len(t.strip()) < 80 or number_count < 3
                 or any(re.search(title, t) for title, _ in groups.values()))
    return dict(accepted=False, code="unreadable_or_ambiguous" if ambiguous else "out_of_scope",
                types=[], reason=("Lecture insuffisante ou état financier non identifiable avec certitude."
                    if ambiguous else "Aucun bilan ni compte de résultat identifiable dans le document."),
                action="Téléversez un bilan ou un compte de résultat lisible avec ses intitulés et montants.")


def inspect_document(path: Path, emit, *, standard="NCT", extractor=None, reader_options=None):
    """Read every native page before rejecting; OCR is required for incomplete reads.

    The optional OCR output is reused by Tier 1. Scans need reading before their
    scope can be known; the gate still runs before the accounting cascade.
    """
    path = Path(path)
    emit(event("identification", "running", "Vérification du fichier et lecture initiale"))
    suffix = path.suffix.lower()
    if suffix not in {".pdf", ".docx", ".jpg", ".jpeg", ".png", ".tiff", ".bmp"}:
        raise DocumentRejected(dict(code="unsupported_format", reason="Format non pris en charge.",
                                    action="Utilisez PDF, DOCX ou une image.", accepted=False))
    text, complete, pages = "", False, None
    try:
        if suffix == ".pdf":
            import fitz
            with fitz.open(path) as doc:
                if not doc.is_pdf:
                    raise ValueError("Extension PDF incompatible avec le contenu")
                parts = [page.get_text() for page in doc]
                pages = len(parts)
                text = "\n".join(parts)
                complete = bool(parts) and all(len(p.strip()) >= 40 for p in parts)
        elif suffix == ".docx":
            from docx import Document
            try:
                doc = Document(path)
            except Exception as exc:
                raise ValueError("DOCX invalide") from exc
            text = "\n".join([p.text for p in doc.paragraphs] +
                             [" ".join(c.text for c in r.cells) for t in doc.tables for r in t.rows])
            complete = not any("image" in r.reltype for r in doc.part.rels.values())
        else:
            from PIL import Image
            with Image.open(path) as img:
                img.verify()
    except (OSError, ValueError, RuntimeError, KeyError, BadZipFile) as exc:
        raise DocumentRejected(dict(code="invalid_file", reason="Fichier illisible, corrompu ou protégé.",
                                    action="Vérifiez le fichier puis déposez une copie lisible.", accepted=False)) from exc
    emit(event("identification", "completed", "Format et contenu initial vérifiés",
               extension=suffix, size_bytes=path.stat().st_size, pages=pages))
    emit(event("admissibility", "running", "Recherche d’un bilan ou d’un compte de résultat"))
    report = classify(text, complete=complete)
    cached = None
    if not report["accepted"] and not complete:
        emit(event("admissibility", "running", "Lecture OCR nécessaire pour vérifier l’admissibilité"))
        if extractor is None:
            from extraction.docling_extractor import DoclingExtractor
            from PIL import Image
            reader = DoclingExtractor(accounting_standard=standard, **(reader_options or {}))
            if suffix == ".pdf":
                cached = reader.extract_from_pdf(path)
            elif suffix == ".docx":
                cached = reader.extract_from_docx(path)
            else:
                with Image.open(path) as img:
                    cached = reader.extract_from_image(img)
        else:
            cached = extractor(path)
        raw = (cached.metadata or {}).get("raw_snapshot") or {}
        text += "\n" + raw.get("text", "")
        floor = (cached.metadata or {}).get("read_floor") or {}
        complete = bool(raw.get("text")) and not floor.get("flagged", False)
        report = classify(text, complete=complete)
    report.update(extension=suffix, pages=pages, characters_read=len(text),
                  reading="ocr" if cached is not None else "native")
    emit(event("admissibility", "completed" if report["accepted"] else "rejected",
               report["reason"], report=report))
    if not report["accepted"]:
        raise DocumentRejected(report)
    return report, cached
