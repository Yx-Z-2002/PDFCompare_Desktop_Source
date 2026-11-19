"""End-to-end regression tests using generated, real PDF input files.

Run: python tests/regression_service.py
Use PDFCOMPARE_APP_DIR to test another app directory.
"""
from __future__ import annotations
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get('PDFCOMPARE_APP_DIR', str(ROOT / 'improved' / 'app')))
import service
from extract_requirements import compile_query, extract_pdf
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from PIL import Image, ImageDraw


def make_pdf(path, pages):
    c = canvas.Canvas(str(path), pagesize=(600, 800))
    for elements in pages:
        c.setFont('Helvetica', 11)
        for element in elements:
            if element[0] == 'text':
                _, x, y, text = element
                c.drawString(x, y, text)
            elif element[0] == 'line':
                c.line(*element[1:])
            elif element[0] == 'raster':
                im = Image.new('RGB', (850, 1050), 'white')
                ImageDraw.Draw(im).text((40, 40), element[1], fill='black')
                c.drawImage(ImageReader(im), 25, 25, width=550, height=750)
            else:
                raise AssertionError(element)
        c.showPage()
    c.save()
    return path


def block(item_id='AT-X-1', title='Safety limit', desc='The limit is 5 mm.', value='XGen', y=735):
    return [('text', 45, y, item_id + ': ' + title),
            ('text', 45, y - 23, desc),
            ('text', 45, y - 48, 'Require Key: ' + value)]


def table(label='Require Key', value='XGen', top=685, bottom=655):
    return [('line', 45, top, 550, top), ('line', 45, bottom, 550, bottom),
            ('line', 45, top, 45, bottom), ('line', 210, top, 210, bottom),
            ('line', 550, top, 550, bottom),
            ('text', 51, top - 18, label), ('text', 216, top - 18, value)]


class ServiceRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='pdfcompare-service-')
        self.root = Path(self.tmp.name)
        self.ws = service.Workspace(self.root / 'workspace')

    def tearDown(self):
        self.ws.close()
        self.tmp.cleanup()

    def register(self, path):
        with path.open('rb') as stream:
            return self.ws.register_document(stream, path.stat().st_size, path.name)['id']

    def run_pair(self, left_pages, right_pages, **overrides):
        left = make_pdf(self.root / '输入 A.pdf', left_pages)
        right = make_pdf(self.root / '输入 B.pdf', right_pages)
        config = {'field': 'Require Key', 'query': 'XGen', 'remove_repeated': False,
                  'output_root': str(self.root / '结果 输出'), 'task_name': '检查', **overrides}
        run = self.ws.extract(self.register(left), self.register(right), config)
        reports = [json.loads((Path(run['folder']) / side / 'extraction_report.json').read_text(encoding='utf-8'))
                   for side in ('A', 'B')]
        return run, reports

    def test_equal_and_changed_requirements_end_to_end(self):
        a = block() + block('AT-X-2', 'Other title', 'Value is 7 mm.', y=590)
        b = block() + block('AT-X-2', 'Other title', 'Value is 8 mm.', y=590)
        run, reports = self.run_pair([a], [b])
        self.assertEqual([r['matched_count'] for r in reports], [2, 2])
        result = self.ws.compare(run['id'], False)
        self.assertEqual(result['summary']['same'], 1)
        self.assertEqual(result['summary']['description_changed'], 1)
        directory = Path(run['folder'])
        with zipfile.ZipFile(directory / 'all_results.zip') as z:
            self.assertIsNone(z.testzip())
            self.assertIn('comparison/comparison.csv', z.namelist())
            self.assertIn('A/extraction_report.json', z.namelist())

    def test_grid_table_extracts_right_hand_value(self):
        a = [('text', 45, 735, 'AT-X-1: Grid'), ('text', 45, 712, 'Description')]
        a += table()
        run, reports = self.run_pair([a], [a])
        self.assertEqual(reports[0]['items'][0]['field_value'], 'XGen')
        self.assertTrue(reports[0]['items'][0]['matched'])
        self.assertTrue(reports[0]['items'][0]['field_evidence'])
        self.assertEqual(reports[0]['items'][0]['metadata_pages'], [1])

    def test_no_matches_does_not_claim_complete_agreement(self):
        run, reports = self.run_pair([block(value='Other')], [block(value='Other')])
        result = self.ws.compare(run['id'], False)
        self.assertTrue(result['summary']['requires_review'])
        self.assertEqual(result['summary']['same'], 0)
        self.assertTrue(result['summary']['extraction_notices'])

    def test_missing_query_field_is_unresolved_not_rejected(self):
        missing = block()[:2] + [('text', 45, 687, 'Risk Related: Yes')]
        run, reports = self.run_pair([missing], [block()])
        self.assertEqual(reports[0]['coverage']['unresolved_count'], 1)
        self.assertEqual(reports[0]['coverage']['rejected_count'], 0)
        result = self.ws.compare(run['id'], False)
        self.assertTrue(result['summary']['requires_review'])

    def test_duplicate_ids_preserved_and_flagged(self):
        a = block() + block('AT-X-1', 'Duplicate entry', y=590)
        run, reports = self.run_pair([a], [block()])
        result = self.ws.compare(run['id'], False)
        self.assertEqual(result['summary']['duplicate_ids'], 1)
        self.assertEqual(result['summary']['same'], 0)
        with (Path(run['folder']) / 'comparison' / 'issues.csv').open(encoding='utf-8-sig') as f:
            self.assertEqual(len(list(csv.DictReader(f))), 2)

    def test_formula_escaping_does_not_change_comparison_text(self):
        a = block(desc='=1+2')
        run, reports = self.run_pair([a], [a])
        self.assertEqual(reports[0]['items'][0]['description'], '=1+2')
        result = self.ws.compare(run['id'], False)
        self.assertEqual(result['summary']['same'], 1)
        with (Path(run['folder']) / 'A' / 'matches.csv').open(encoding='utf-8-sig') as f:
            row = next(csv.DictReader(f))
        self.assertEqual(row['具体描述'], "'=1+2")

    def test_query_fragmentation_and_option_boundaries(self):
        q = compile_query('XGen')
        for text in ('XGen', 'X Gen', 'X\nG e n', '[XGen]', 'Other; XGen; More'):
            with self.subTest(text=text): self.assertIsNotNone(q.search(text))
        for text in ('XGen2', 'PreXGen', 'OtherXGenOption'):
            with self.subTest(text=text): self.assertIsNone(q.search(text))

    def test_scan_only_pdf_has_explicit_warning(self):
        scan = [('raster', 'AT-X-1: Visible text only in image\nRequire Key: XGen')]
        run, reports = self.run_pair([scan], [block()])
        self.assertEqual(reports[0]['items'], [])
        self.assertTrue(any('OCR' in w or '扫描' in w for w in reports[0]['warnings']))
        result = self.ws.compare(run['id'], False)
        self.assertTrue(result['summary']['requires_review'])

    def test_scan_gap_must_not_assign_following_attributes_to_old_item(self):
        p = make_pdf(self.root / 'scan-gap.pdf', [
            block('AT-X-1', value='Other'),
            [('raster', 'AT-X-2: This heading is scanned')],
            [('text', 45, 735, 'Require Key: XGen')],
        ])
        report = extract_pdf(p, compile_query('XGen'), field_name='Require Key')
        old = next(item for item in report['items'] if item['item_id'] == 'AT-X-1')
        self.assertFalse(old['matched'], 'A field after an unreadable page must not be assigned to an older requirement')
        self.assertNotIn('XGen', old['field_value'])
        self.assertTrue(report['warnings'])

    def test_repeated_prose_without_separator_is_not_removed(self):
        pages = []
        for i in range(2):
            pages.append([('text', 45, 780, f'AT-X-{i+1}: Top requirement'),
                          ('text', 45, 762, 'Repeated but meaningful description.'),
                          ('text', 45, 730, 'Require Key: XGen')])
        p = make_pdf(self.root / 'repeated.pdf', pages)
        report = extract_pdf(p, compile_query('XGen'), field_name='Require Key', remove_repeated=True)
        self.assertEqual(len(report['items']), 2)
        for item in report['items']:
            self.assertIn('Repeated but meaningful', item['description'])
            self.assertTrue(item['matched'])

    def test_inputs_unchanged_and_output_retained_after_close(self):
        run, reports = self.run_pair([block()], [block()])
        input_path = self.root / '输入 A.pdf'
        before = hashlib.sha256(input_path.read_bytes()).hexdigest()
        temp_paths = [doc['path'] for doc in self.ws.documents.values()]
        self.ws.close()
        self.assertEqual(hashlib.sha256(input_path.read_bytes()).hexdigest(), before)
        self.assertTrue((Path(run['folder']) / 'A' / 'matches.csv').exists())
        self.assertTrue(all(not p.exists() for p in temp_paths))

    def test_invalid_input_and_truncated_upload_cleanup(self):
        with self.assertRaises(ValueError):
            self.ws.register_document(io.BytesIO(b'not a PDF'), 9, 'bad.pdf')
        with self.assertRaises(ValueError):
            self.ws.register_document(io.BytesIO(b'%PDF-'), 2000, 'truncated.pdf')
        self.assertEqual(list(self.ws.temp.glob('*.pdf')), [])


    def audit_for(self, run):
        self.ws.compare(run['id'], False)
        return json.loads((Path(run['folder']) / 'comparison' / 'comparison_audit.json').read_text(encoding='utf-8'))

    def test_comparison_audit_explains_filter_rejection(self):
        run, reports = self.run_pair([block(value='XGen')], [block(value='Other')])
        audit = self.audit_for(run)
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]['item_id'], 'AT-X-1')
        self.assertEqual(audit[0]['missing_side'], 'B')
        self.assertEqual(audit[0]['field_values'], ['Other'])
        self.assertEqual(audit[0]['pages'], [1])
        self.assertIn('未满足本次查询条件', audit[0]['reason'])
        with (Path(run['folder']) / 'comparison' / 'comparison.csv').open(encoding='utf-8-sig') as f:
            row = next(csv.DictReader(f))
        self.assertIn('未满足本次查询条件', row['备注'])

    def test_comparison_audit_explains_missing_field(self):
        run, reports = self.run_pair([block()], [block()[:2]])
        audit = self.audit_for(run)
        self.assertEqual(len(audit), 1)
        self.assertIn('未能判断是否命中', audit[0]['reason'])
        self.assertEqual(audit[0]['pages'], [1])
        self.assertEqual(audit[0]['field_values'], [''])

    def test_comparison_audit_locates_body_reference_without_creating_record(self):
        right = [('text', 45, 735, 'This paragraph refers to AT-X-1 for background only.')]
        run, reports = self.run_pair([block()], [right])
        self.assertEqual(reports[1]['items'], [])
        audit = self.audit_for(run)
        self.assertEqual(audit[0]['pages'], [1])
        self.assertIn('可能为正文或引用', audit[0]['reason'])
        self.assertIn('未识别到该需求条目', audit[0]['reason'])

    def test_comparison_audit_does_not_confuse_id_prefix(self):
        right = [('text', 45, 735, 'This paragraph refers to AT-X-10 only.')]
        run, reports = self.run_pair([block()], [right])
        audit = self.audit_for(run)
        self.assertEqual(audit[0]['pages'], [], 'AT-X-10 must not count as the identifier AT-X-1')
        self.assertNotIn('发现该编号', audit[0]['reason'])

    def test_original_search_handles_split_ids_and_scanned_pages(self):
        p = make_pdf(self.root / 'search.pdf', [
            [('text', 45, 735, 'Reference: AT - X -'), ('text', 45, 715, '1 has moved.')],
            [('raster', 'This scanned page visually says AT-X-1')],
            [('text', 45, 735, 'Second reference: AT-X-1.')],
        ])
        progress = []
        result = self.ws.search_document(self.register(p), 'AT-X-1', lambda *args: progress.append(args))
        self.assertEqual([h['page'] for h in result['hits']], [1, 3])
        self.assertEqual(result['scanned_pages'], [2])
        self.assertEqual([h['occurrences'] for h in result['hits']], [1, 1])
        self.assertEqual(len(progress), 3)
        self.assertIn('不受本次裁切', result['note'])
        self.assertIn('引用', result['note'])

    def test_original_search_rejects_empty_or_oversized_query(self):
        p = make_pdf(self.root / 'search-validation.pdf', [block()])
        identifier = self.register(p)
        for query in (' ', '\t\n', 'X' * 501):
            with self.subTest(query=query[:20]), self.assertRaises(ValueError):
                self.ws.search_document(identifier, query)

    def test_windows_reserved_task_names_are_rejected(self):
        for name in ('CON', ' con ', 'CON.txt', 'PRN', 'AUX', 'NUL', 'COM1', 'lpt9.ext'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'Windows 保留名称'):
                service.checked_config({'field': 'Require Key', 'query': 'XGen', 'task_name': name})
        for name in ('CONTEXT', 'COM10', '检查任务'):
            with self.subTest(name=name):
                self.assertEqual(service.checked_config({'field': 'Require Key', 'query': 'XGen', 'task_name': name})['task_name'], name)


if __name__ == '__main__':
    unittest.main(verbosity=2)
