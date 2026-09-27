"""Admission, pipeline short circuit, raw provenance and real milestones."""
import asyncio
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import fitz
import pandas as pd
import pytest
from docx import Document

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from document_intake import DocumentRejected, classify, inspect_document, snapshot

FINANCIAL = 'BILAN au 31 decembre 2025\nActif 1000\nPassif 500\nCapitaux propres 500'
INVOICE = 'Facture numero 125 du fournisseur. Prestations de conseil et support informatique. Total 1500 taxe 300 net 1800.'


def pdf(tmp_path, texts):
    path = tmp_path / 'document.pdf'
    with fitz.open() as doc:
        for text in texts:
            doc.new_page().insert_text((60, 60), text)
        doc.save(path)
    return path


@pytest.mark.parametrize('text,code', [
    (FINANCIAL, 'admitted'),
    ('Compte de resultat 2025\nProduits 1000\nCharges 600\nResultat 400', 'admitted'),
    (INVOICE, 'out_of_scope'),
    ('Bilan du projet sans montants financiers', 'unreadable_or_ambiguous'),
    ('', 'unreadable_or_ambiguous'),
])
def test_classification(text, code):
    assert classify(text)['code'] == code


def test_native_rejection_does_not_run_ocr(tmp_path):
    events = []
    with pytest.raises(DocumentRejected) as exc:
        inspect_document(pdf(tmp_path, [INVOICE]), events.append,
                         extractor=lambda _: pytest.fail('OCR unnecessary'))
    assert exc.value.report['code'] == 'out_of_scope'
    assert events[-1]['state'] == 'rejected'


def test_late_statement_not_rejected_from_cover(tmp_path):
    report, cached = inspect_document(pdf(tmp_path, [INVOICE] * 4 + [FINANCIAL]), lambda _: None)
    assert report['accepted'] and report['pages'] == 5
    assert cached is None


def test_scan_reading_reused_and_raw_only(tmp_path):
    from PIL import Image
    path = tmp_path / 'scan.png'
    Image.new('RGB', (60, 60)).save(path)
    reading = SimpleNamespace(text=INVOICE, metadata={'raw_snapshot': snapshot(FINANCIAL, [], 'test')})
    report, cached = inspect_document(path, lambda _: None, extractor=lambda _: reading)
    assert report['accepted'] and cached is reading


def test_empty_scan_is_ambiguous_not_nonfinancial(tmp_path):
    from PIL import Image
    path = tmp_path / 'scan.png'
    Image.new('RGB', (60, 60)).save(path)
    with pytest.raises(DocumentRejected) as exc:
        inspect_document(path, lambda _: None, extractor=lambda _: SimpleNamespace(
            metadata={'raw_snapshot': snapshot('', [], 'test'), 'read_floor': {'flagged': True}}))
    assert exc.value.report['code'] == 'unreadable_or_ambiguous'


def test_docx_native_table_admitted(tmp_path):
    path = tmp_path / 'bilan.docx'
    doc = Document()
    doc.add_paragraph('Bilan 2025')
    table = doc.add_table(rows=3, cols=2)
    for row, values in zip(table.rows, [('Actif', '1000'), ('Passif', '500'), ('Capitaux propres', '500')]):
        for cell, value in zip(row.cells, values):
            cell.text = value
    doc.save(path)
    report, cached = inspect_document(path, lambda _: None)
    assert report['accepted'] and cached is None


def test_corrupt_pdf_has_actionable_cause(tmp_path):
    path = tmp_path / 'broken.pdf'
    path.write_bytes(b'bad pdf')
    with pytest.raises(DocumentRejected) as exc:
        inspect_document(path, lambda _: None)
    assert exc.value.report['code'] == 'invalid_file'


def test_snapshot_is_detached_preserves_duplicates():
    frame = pd.DataFrame([['1 200', '3,00']], columns=['N', 'N'])
    raw = snapshot('raw', [frame], 'test')
    frame.iloc[0, 0] = 'corrected'
    assert raw['tables'][0]['data'][0][0] == '1 200'
    assert raw['tables'][0]['columns'] == ['N', 'N']
    assert len(raw['sha256']) == 64


def test_pipeline_gate_prevents_accounting(tmp_path, monkeypatch):
    from pipeline import FinAlzePipeline
    pipe = FinAlzePipeline()
    monkeypatch.setattr(pipe, '_run_tier_cascade', lambda *a, **k: pytest.fail('Must stop before cascade'))
    with pytest.raises(DocumentRejected):
        pipe.process_document(pdf(tmp_path, [INVOICE]))


def test_pipeline_emits_real_milestones(tmp_path, monkeypatch):
    from pipeline import FinAlzePipeline, ExtractionResult
    pipe = FinAlzePipeline()
    result = ExtractionResult(FINANCIAL, [{'Actif': '1000'}], .8, 0, {}, {})
    monkeypatch.setattr(pipe, '_run_tier_cascade', lambda *a, **k: result)
    monkeypatch.setattr(pipe, '_attach_statements', lambda r: None)
    monkeypatch.setattr(pipe, '_attach_document_unit', lambda r: None)
    events = []
    output = pipe.process_document(pdf(tmp_path, [FINANCIAL]), progress_callback=events.append)
    assert events[-1]['stage'] == 'review' and events[-1]['state'] == 'waiting'
    assert output.metadata['workflow_events'] == events
    assert output.metadata['admission']['accepted']


def test_job_summary_exposes_progress_without_financial_result():
    from api.models import Job, JobStatus
    from api.schemas import JobSummary
    job = Job(id=uuid.uuid4(), status=JobStatus.running, hitl_required=False,
              result={'progress': [{'stage': 'admissibility', 'state': 'running'}], 'tables': ['private']})
    response = JobSummary.model_validate(job).model_dump()
    assert response['progress'][0]['stage'] == 'admissibility'
    assert 'result' not in response and 'tables' not in response


def test_rejection_is_not_retried(monkeypatch):
    from worker import runtime
    from worker.tasks import run_extraction
    async def context(_):
        return {'storage_key': 'x', 'filename': 'x.pdf', 'standard': 'NCT'}
    stored = []
    async def progress(*args): pass
    async def failure(job, exc): stored.append(str(exc))
    def reject(*a, **kw):
        raise DocumentRejected(dict(code='out_of_scope', reason='Hors perimetre', action='Autre fichier'))
    monkeypatch.setattr(runtime, 'load_job_context', context)
    monkeypatch.setattr(runtime, 'save_progress', progress)
    monkeypatch.setattr(runtime, 'save_failure', failure)
    monkeypatch.setattr(runtime, 'fetch_document', lambda *a: Path('x.pdf'))
    monkeypatch.setattr(runtime, 'get_pipeline', lambda: SimpleNamespace(process_document=reject))
    monkeypatch.setattr(runtime, 'configure_pipeline', lambda *a: None)
    assert run_extraction.run('job')['rejection']['code'] == 'out_of_scope'
    assert len(stored) == 1


def test_progress_persists_then_terminal_result_keeps_history(tmp_path, monkeypatch):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from api.db import Base
    from api.models import Job, JobStatus, Org, Document as StoredDocument
    from worker import runtime
    url = 'sqlite+aiosqlite:///' + str(tmp_path / 'progress.db')
    monkeypatch.setattr(runtime, '_engine', lambda: create_async_engine(url))
    job_id, org_id, doc_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    events = [{'stage': 'extraction', 'state': 'running', 'message': 'OCR'}]
    async def scenario():
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine)
        async with sm() as session:
            session.add(Org(id=org_id, name='tenant'))
            session.add(StoredDocument(id=doc_id, org_id=org_id, filename='b.pdf',
                                       storage_key='b.pdf', accounting_standard='NCT'))
            session.add(Job(id=job_id, org_id=org_id, document_id=doc_id, status=JobStatus.running))
            await session.commit()
        await runtime.save_progress(str(job_id), events)
        async with sm() as session:
            assert (await session.get(Job, job_id)).progress == events
        await runtime.save_success(str(job_id), {'metadata': {'workflow_events': events}}, 0, .9, True)
        await runtime.save_progress(str(job_id), [{'stage': 'stale'}])
        async with sm() as session:
            job = await session.get(Job, job_id)
            assert job.status == JobStatus.done and job.progress == events
        await engine.dispose()
    asyncio.run(scenario())


def test_docling_snapshot_precedes_corrections(monkeypatch):
    from extraction.docling_extractor import DoclingExtractor
    import preprocessing.text_postprocessor as pp
    source = pd.DataFrame([['raw label', '1 000']], columns=['label', 'N'])
    table = SimpleNamespace(export_to_dataframe=lambda **_: source.copy(), prov=[])
    doc = SimpleNamespace(tables=[table], texts=[], pages={},
                          export_to_markdown=lambda: 'raw label 1 000')
    def mutate(df, *args):
        df.iloc[0, 0] = 'corrected label'
        return df
    monkeypatch.setattr(pp, 'postprocess_dataframe', mutate)
    monkeypatch.setattr(pp, 'postprocess_text_with_stats', lambda *a: ('corrected text', {}))
    result = DoclingExtractor()._process_result(SimpleNamespace(document=doc))
    raw = result.metadata['raw_snapshot']
    assert raw['tables'][0]['data'][0][0] == 'raw label'
    assert raw['text'] == 'raw label 1 000'
    assert result.tables[0].iloc[0, 0] == 'corrected label'



def test_docx_extractor_preserves_native_tables(tmp_path):
    from extraction.docling_extractor import DoclingExtractor
    path = tmp_path / 'native.docx'
    doc = Document()
    doc.add_paragraph('Bilan')
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = 'Actif'
    table.cell(0, 1).text = '1 000'
    doc.save(path)
    result = DoclingExtractor().extract_from_docx(path)
    assert result.tables[0].iloc[0, 1] == '1 000'
    raw = result.metadata['raw_snapshot']
    assert '1 000' in raw['text'] and raw['tables'][0]['data'][0][1] == '1 000'


def test_corrupt_docx_rejected(tmp_path):
    path = tmp_path / 'broken.docx'
    path.write_bytes(b'not a docx')
    with pytest.raises(DocumentRejected) as exc:
        inspect_document(path, lambda _: None)
    assert exc.value.report['code'] == 'invalid_file'
