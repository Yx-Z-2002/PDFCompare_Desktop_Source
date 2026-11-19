"""Real-PDF field recovery and uncertainty evidence, including hard negatives."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'improved' / 'app'))
from reportlab.pdfgen.canvas import Canvas
import extract_requirements as ex
from task_control import TaskCancelled


class FieldRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pdfcompare-fields23-')
        self.path = Path(self.temp.name) / 'input.pdf'

    def tearDown(self):
        self.temp.cleanup()

    def draw(self, rows, next_pages=()):
        c = Canvas(str(self.path), pagesize=(600, 800))
        for page in [rows, *next_pages]:
            c.setFont('Helvetica', 11)
            for x, y, text in page:
                c.drawString(x, y, text)
            c.showPage()
        c.save()

    def run_extract(self, field='Require Key', **kwargs):
        return ex.extract_pdf(self.path, ex.compile_query('XGen'), field_name=field, **kwargs)

    def one(self, rows, field='Require Key', **kwargs):
        self.draw([(45, 750, 'AT-X-1: Requirement'), *rows])
        report = self.run_extract(field, **kwargs)
        self.assertEqual(len(report['items']), 1)
        return report['items'][0]

    def test_same_line_separate_pairs(self):
        item = self.one([(45, 700, 'Risk Related: No'), (300, 700, 'Require Key: XGen')])
        self.assertTrue(item['matched'])
        self.assertEqual(item['field_value'], 'XGen')
        self.assertEqual(item['attributes']['Risk Related'], 'No')

    def test_same_line_pairs_without_colon(self):
        item = self.one([(45, 700, 'Risk Related'), (180, 700, 'No'),
                         (300, 700, 'Require Key'), (450, 700, 'XGen')])
        self.assertTrue(item['matched'])
        self.assertEqual(item['attributes']['Risk Related'], 'No')

    def test_second_pair_empty_label_and_value_below(self):
        item = self.one([(45, 700, 'Risk Related: No'), (300, 700, 'Require Key:'),
                         (300, 680, 'XGen')])
        self.assertEqual(item['field_value'], 'XGen')

    def test_wrapped_label_value_at_first_row_right(self):
        item = self.one([(45, 700, 'Require'), (240, 700, 'XGen'), (45, 685, 'Key')])
        self.assertTrue(item['matched'])
        self.assertEqual(item['field_value'], 'XGen')

    def test_wrapped_label_does_not_take_other_fields_value(self):
        item = self.one([(45, 700, 'Require'), (300, 700, 'Test Level: XGen'),
                         (45, 685, 'Key')])
        self.assertFalse(item['matched'])
        self.assertEqual(item['field_status'], 'missing')

    def test_wrapped_label_does_not_take_unknown_field_or_heading(self):
        for right in ('Custom Category: XGen', 'AT-X-2: XGen requirement'):
            with self.subTest(right=right):
                item = self.one([(45, 700, 'Require'), (300, 700, right), (45, 685, 'Key')])
                self.assertFalse(item['matched'])

    def test_label_split_three_lines(self):
        item = self.one([(45, 700, 'Requirement'), (45, 685, 'Classifi'),
                         (45, 670, 'cation: XGen')], field='Requirement Classification')
        self.assertTrue(item['matched'])
        self.assertEqual(item['field_value'], 'XGen')

    def test_no_grid_index_column(self):
        item = self.one([(45, 700, '1'), (90, 700, 'Require Key'), (250, 700, 'XGen')])
        self.assertTrue(item['matched'])

    def test_index_with_explicit_field_value(self):
        item = self.one([(45, 700, '1.'), (90, 700, 'Require Key: XGen')])
        self.assertTrue(item['matched'])

    def test_numbered_prose_is_not_a_field(self):
        item = self.one([(45, 700, '1'), (90, 700, 'Require Key is discussed as XGen here.')])
        self.assertFalse(item['matched'])
        self.assertEqual(item['field_status'], 'missing')

    def test_narrative_mention_does_not_become_field(self):
        item = self.one([(45, 700, 'The description mentions Require Key: XGen and nothing else.')])
        self.assertFalse(item['matched'])
        self.assertEqual(item['field_status'], 'missing')
        self.assertIn('label_text_unparsed', item['field_diagnostic']['reason_codes'])
        self.assertEqual(item['field_diagnostic']['evidence'][0]['page'], 1)

    def test_mention_inside_another_field_value_not_reassigned(self):
        item = self.one([(45, 700, 'Risk Related: Note mentions Require Key: XGen')])
        self.assertFalse(item['matched'])
        self.assertEqual(item['attributes']['Risk Related'], 'Note mentions Require Key: XGen')

    def test_value_named_like_another_field_stays_value(self):
        item = self.one([(45, 700, 'Product Structure'), (240, 700, 'Test Level')], field='Product Structure')
        self.assertEqual(item['field_value'], 'Test Level')
        self.assertNotIn('Test Level', item['attributes'])

    def test_wrapped_label_cannot_cross_next_requirement(self):
        self.draw([(45, 750, 'AT-X-1: First'), (45, 710, 'Require'),
                   (45, 690, 'AT-X-2: Second'), (45, 670, 'Key: XGen')])
        report = self.run_extract()
        self.assertEqual(len(report['items']), 2)
        self.assertTrue(all(not item['matched'] for item in report['items']))

    def test_next_requirement_label_is_not_diagnostic_evidence_for_first(self):
        self.draw([(45, 750, 'AT-X-1: First'), (45, 720, 'Nothing to resolve here.'),
                   (45, 690, 'AT-X-2: Second'), (45, 670, 'Require Key: XGen')])
        first, second = self.run_extract()['items']
        self.assertFalse(first['matched'])
        self.assertTrue(second['matched'])
        self.assertEqual(first['field_diagnostic']['evidence'], [])
        self.assertIn('label_not_located', first['field_diagnostic']['reason_codes'])

    def test_different_label_is_explained_not_aliased(self):
        item = self.one([(45, 700, 'Requirement Key: XGen')])
        self.assertFalse(item['matched'])
        self.assertIn('different_label_present', item['field_diagnostic']['reason_codes'])
        self.assertIn('Requirement Key', item['field_diagnostic']['summary'])

    def test_cropped_label_keeps_missing_with_page_evidence(self):
        item = self.one([(45, 40, 'Require Key: XGen')], bottom_margin=70)
        self.assertFalse(item['matched'])
        self.assertIn('label_text_excluded', item['field_diagnostic']['reason_codes'])
        self.assertEqual(item['field_diagnostic']['evidence'][0]['disposition'], 'cropped')

    def test_cropped_header_label_not_claimed_as_current_item(self):
        self.draw([(45, 780, 'Require Key: XGen'), (45, 700, 'AT-X-1: Body')])
        item = self.run_extract(top_margin=50)['items'][0]
        self.assertFalse(item['matched'])
        self.assertIn('不证明属于本条目', item['field_diagnostic']['summary'])

    def test_all_text_cropped_not_misdiagnosed_scan(self):
        self.draw([(45, 780, 'AT-X-1: Heading'), (45, 765, 'Require Key: XGen')])
        report = self.run_extract(top_margin=80)
        self.assertEqual(report['diagnostics'][0]['status'], 'cropped_empty')
        self.assertTrue(report['field_label_sightings'])
        self.assertTrue(any('原页有文字' in message for message in report['warnings']))

    def test_empty_value_is_unknown_and_distinct(self):
        item = self.one([(45, 700, 'Require Key:'), (45, 680, 'Test Level: Unit')])
        self.assertEqual(item['field_status'], 'empty')
        self.assertFalse(item['matched'])
        self.assertIn('value_not_resolved', item['field_diagnostic']['reason_codes'])

    def test_table_error_falls_back_to_text_and_recovery(self):
        self.draw([(45, 750, 'AT-X-1: Requirement'), (45, 700, 'Risk Related: No'),
                   (300, 700, 'Require Key: XGen')])
        with patch.object(ex, 'metadata_events', side_effect=RuntimeError('fixture table error')):
            report = self.run_extract()
        self.assertTrue(report['items'][0]['matched'])
        self.assertTrue(any('表格识别失败' in x for x in report['warnings']))

    def test_cancellation_is_not_swallowed_by_table_fallback(self):
        self.draw([(45, 750, 'AT-X-1: Requirement'), (45, 700, 'Require Key: XGen')])
        with patch.object(ex, 'metadata_events', side_effect=TaskCancelled()):
            with self.assertRaises(TaskCancelled):
                self.run_extract()

    def test_cancel_during_page_read_stops_before_next_page(self):
        self.draw([(45, 750, 'AT-X-1: First')], [[(45, 750, 'AT-X-2: Second')]])
        cancel = {'requested': False}
        def progress(page, total):
            cancel['requested'] = True
        def check():
            if cancel['requested']:
                raise TaskCancelled()
        with self.assertRaises(TaskCancelled):
            self.run_extract(cancel_check=check, progress=progress)


if __name__ == '__main__':
    unittest.main(verbosity=2)
