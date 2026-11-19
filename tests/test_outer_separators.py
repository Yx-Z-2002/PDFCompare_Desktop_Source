"""2.2: first top rule / last bottom rule, independent of content evidence."""
from __future__ import annotations
import json
from pathlib import Path
import sys
import tempfile
import unittest

import pdfplumber
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen.canvas import Canvas

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'improved' / 'app'))
from margins import separator_analysis, separator_bounds
from extract_requirements import compile_query, effective_page_bbox, extract_pdf, main
from service import DEFAULTS, Workspace, checked_config


class OutermostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='pdfcompare-outermost-')
        self.root = Path(self.tmp.name)
        self.path = self.root / 'input.pdf'

    def tearDown(self):
        self.tmp.cleanup()

    def draw(self, callback):
        c = Canvas(str(self.path), pagesize=(600, 800))
        c.setFont('Helvetica', 11)
        callback(c)
        c.save()
        return self.path

    def text(self, c, y, text):
        c.drawString(50, 800-y, text)

    def line(self, c, y, x0=40, x1=560):
        c.line(x0, 800-y, x1, 800-y)

    def analysis(self, number=0, **options):
        with pdfplumber.open(self.path) as doc:
            page = doc.pages[number]
            return separator_analysis(page, bounds=effective_page_bbox(page),
                                      **{'mode': 'outermost', **options})

    def body(self, c):
        self.text(c, 200, 'AT-X-1: Body requirement')
        self.text(c, 230, 'This body must be preserved.')
        self.text(c, 260, 'Require Key: XGen')

    def test_first_top_and_last_bottom_of_multiple_lines(self):
        def draw(c):
            for y in [55, 90, 125, 670, 705, 740]:
                self.line(c, y)
            self.body(c)
        self.draw(draw)
        r = self.analysis()
        self.assertEqual((r['top'], r['bottom']), (56, 61))
        self.assertEqual([x['y'] for x in r['candidates'] if x['accepted']], [55, 740])
        self.assertEqual(r['mode'], 'outermost')
        self.assertEqual((self.analysis(mode='conservative')['top'], self.analysis(mode='conservative')['bottom']), (0, 0))

    def test_boxed_header_and_footer_are_eligible(self):
        def draw(c):
            self.line(c, 65); self.line(c, 735)
            c.line(40, 780, 40, 720)
            c.line(560, 90, 560, 20)
            self.text(c, 35, 'Header'); self.text(c, 770, 'Footer')
            self.body(c)
        self.draw(draw)
        self.assertEqual((self.analysis()['top'], self.analysis()['bottom']), (66, 66))
        self.assertEqual((self.analysis(mode='conservative')['top'], self.analysis(mode='conservative')['bottom']), (0, 0))

    def test_no_text_outside_no_longer_blocks_boundary(self):
        self.draw(lambda c: (self.line(c, 60), self.body(c)))
        self.assertEqual(self.analysis()['top'], 61)
        self.assertEqual(self.analysis()['removed_text']['top'], [])
        self.assertEqual(self.analysis(mode='conservative')['top'], 0)

    def test_near_text_is_chosen_with_inspectable_warning(self):
        def draw(c):
            self.text(c, 35, 'Header')
            self.line(c, 65)
            self.text(c, 68, 'Text crossing the boundary')
            self.body(c)
        self.draw(draw)
        r = self.analysis()
        self.assertEqual(r['top'], 66)
        self.assertTrue(any('穿过文字或图像' in message for message in r['warnings']))
        self.assertEqual(self.analysis(mode='conservative')['top'], 0)

    def test_near_rule_removed_text_uses_actual_padded_crop(self):
        def draw(c):
            self.text(c, 58.5, 'Close header')
            self.line(c, 60)
            self.body(c)
            self.line(c, 740)
            self.text(c, 748, 'Close footer')
        self.draw(draw)
        r = self.analysis()
        self.assertIn('Close header', r['removed_text']['top'])
        self.assertIn('Close footer', r['removed_text']['bottom'])
        report = extract_pdf(self.path, compile_query('XGen'), field_name='Require Key',
                             auto_separators=True, separator_mode='outermost')
        clean = report['page_cleaning'][0]
        self.assertEqual(clean['auto_removed_lines'], 2)
        self.assertEqual(clean['cropped_lines'], 2)

    def test_single_bottom_rule_only_crops_bottom(self):
        self.draw(lambda c: (self.body(c), self.line(c, 740)))
        self.assertEqual((self.analysis()['top'], self.analysis()['bottom']), (0, 61))

    def test_no_rules_keeps_full_page(self):
        self.draw(self.body)
        self.assertEqual((self.analysis()['top'], self.analysis()['bottom']), (0, 0))

    def test_middle_rule_is_never_promoted_to_header(self):
        self.draw(lambda c: (self.body(c), self.line(c, 400)))
        r = self.analysis()
        self.assertEqual((r['top'], r['bottom']), (0, 0))
        self.assertIn('正文区域', r['candidates'][0]['reason'])

    def test_short_rules_need_explicit_width_adjustment(self):
        self.draw(lambda c: (self.line(c, 60, 150, 450), self.body(c)))
        self.assertEqual(self.analysis()['top'], 0)
        self.assertEqual(self.analysis(min_width=40)['top'], 61)

    def test_rules_outside_default_zone_need_explicit_zone_adjustment(self):
        self.draw(lambda c: (self.line(c, 180), self.body(c)))
        self.assertEqual(self.analysis()['top'], 0)
        self.assertEqual(self.analysis(zone=25)['top'], 181)

    def test_dashed_and_filled_rules_are_supported(self):
        def draw(c):
            c.setDash(4, 3); self.line(c, 60); c.setDash()
            c.rect(40, 59, 520, 2, fill=1, stroke=0)
            self.body(c)
        self.draw(draw)
        r = self.analysis()
        self.assertEqual(r['top'], 61)
        self.assertGreater(r['bottom'], 61)
        self.assertEqual({x['kind'] for x in r['candidates'] if x['accepted']},
                         {'dashed_line', 'filled_rectangle'})

    def test_separately_drawn_dash_fragments_form_one_rule(self):
        def draw(c):
            self.text(c, 30, 'Header')
            for x in range(40, 560, 10):
                self.line(c, 60, x, x+6)
            self.body(c)
        self.draw(draw)
        for mode in ('outermost', 'conservative'):
            r = self.analysis(mode=mode)
            self.assertEqual(r['top'], 61)
            chosen = [c for c in r['candidates'] if c['accepted']]
            self.assertEqual(len(chosen), 1)
            self.assertEqual(chosen[0]['kind'], 'segmented_line')
            self.assertEqual(chosen[0]['fragment_count'], 52)

    def test_two_contiguous_strokes_can_make_a_full_width_rule(self):
        self.draw(lambda c: (self.line(c, 60, 40, 300), self.line(c, 60, 300, 560), self.body(c)))
        self.assertEqual(self.analysis()['top'], 61)

    def test_large_gaps_do_not_join_unrelated_ornaments(self):
        def draw(c):
            for x in range(40, 560, 40):
                self.line(c, 60, x, x+4)
            self.body(c)
        self.draw(draw)
        self.assertEqual(self.analysis()['top'], 0)

    def test_fragmented_table_rules_still_rejected_in_conservative_mode(self):
        def draw(c):
            self.text(c, 30, 'Header')
            for y in (60, 90, 120):
                for x in range(40, 560, 10):
                    self.line(c, y, x, x+6)
            self.body(c)
        self.draw(draw)
        self.assertEqual(self.analysis(mode='conservative')['top'], 0)
        self.assertEqual(self.analysis()['top'], 61)

    def test_cropbox_coordinates_and_180_rotation(self):
        self.draw(lambda c: (self.line(c, 100), self.line(c, 700), self.body(c)))
        reader = PdfReader(self.path); page = reader.pages[0]
        page.cropbox.lower_left = (20, 40); page.cropbox.upper_right = (580, 760)
        page.rotate(180)
        writer = PdfWriter(); writer.add_page(page)
        dest = self.root / 'rotated.pdf'; writer.write(dest); dest.replace(self.path)
        r = self.analysis()
        self.assertEqual(r['visible_bbox'], [20, 40, 580, 760])
        self.assertEqual((r['top'], r['bottom']), (61, 61))

    def test_boundary_rules_keep_valid_nonempty_rectangle(self):
        self.draw(lambda c: (self.line(c, 0), self.line(c, 800), self.body(c)))
        r = self.analysis()
        self.assertGreaterEqual(r['top'], 0); self.assertGreaterEqual(r['bottom'], 0)
        self.assertLess(r['top']+r['bottom'], 800)

    def test_extraction_matches_preview_and_preserves_manual_crop(self):
        def draw(c):
            self.text(c, 25, 'AT-X-900: Header requirement')
            self.text(c, 45, 'Require Key: XGen')
            self.line(c, 65); c.line(40, 780, 40, 720)
            self.body(c)
            self.line(c, 735); self.text(c, 770, 'Footer')
        self.draw(draw)
        r = self.analysis()
        report = extract_pdf(self.path, compile_query('XGen'), field_name='Require Key',
                             auto_separators=True, separator_mode='outermost', bottom_margin=80)
        clean = report['page_cleaning'][0]
        self.assertEqual([i['item_id'] for i in report['items']], ['AT-X-1'])
        self.assertEqual(clean['top_margin'], r['top'])
        self.assertEqual(clean['bottom_margin'], 80)
        self.assertEqual(clean['auto_detection'], {**r, 'enabled': True})

    def test_disabled_mode_records_settings_but_preserves_page(self):
        self.draw(lambda c: (self.line(c, 60), self.body(c)))
        report = extract_pdf(self.path, compile_query('XGen'), field_name='Require Key',
                             auto_separators=False, separator_mode='outermost')
        clean = report['page_cleaning'][0]
        self.assertEqual(clean['crop_bbox'], clean['visible_bbox'])
        self.assertEqual(clean['auto_detection']['mode'], 'outermost')
        self.assertFalse(clean['auto_detection']['enabled'])

    def test_repeated_cleanup_cannot_use_deeper_conservative_line(self):
        def draw(c):
            for i in range(2):
                c.setFont('Helvetica', 10)
                self.text(c, 22, 'Header '+str(i)); self.line(c, 40)
                c.line(40, 780, 40, 755)  # first rule rejected only in conservative mode
                self.text(c, 58, 'AT-X-'+str(i+1)+': Body')
                self.text(c, 75, 'Critical repeated body description')
                self.line(c, 100)
                self.text(c, 140, 'Require Key: XGen')
                c.showPage()
        self.draw(draw)
        report = extract_pdf(self.path, compile_query('XGen'), field_name='Require Key',
                             auto_separators=True, separator_mode='outermost', remove_repeated=True)
        self.assertEqual(len(report['items']), 2)
        self.assertTrue(all('Critical repeated body description' in i['description'] for i in report['items']))
        self.assertTrue(all(c['repeated_removed_lines'] == 0 for c in report['page_cleaning']))

    def test_service_default_and_saved_settings_propagate_mode(self):
        self.draw(lambda c: (self.line(c, 60), self.body(c)))
        ws = Workspace(self.root/'workspace')
        try:
            with self.path.open('rb') as f:
                doc = ws.register_document(f, self.path.stat().st_size, self.path.name)
            run = ws.extract(doc['id'], doc['id'], {'field': 'Require Key', 'query': 'XGen',
                             'auto_separators': True, 'output_root': str(self.root/'output')})
            report = json.loads((Path(run['folder'])/'A/extraction_report.json').read_text())
            saved = json.loads((Path(run['folder'])/'settings.json').read_text())
        finally:
            ws.close()
        self.assertEqual(saved['separator_mode'], 'outermost')
        self.assertEqual(report['page_cleaning'][0]['top_margin'], 61)
        self.assertFalse(DEFAULTS['auto_separators'])

    def test_config_rejects_unknown_mode_and_wrapper_forwards_mode(self):
        self.draw(lambda c: (self.line(c, 60), self.body(c)))
        with self.assertRaises(ValueError):
            checked_config({'field': 'Require Key', 'query': 'XGen', 'separator_mode': 'guess'})
        with pdfplumber.open(self.path) as doc:
            self.assertEqual(separator_bounds(doc.pages[0], mode='outermost'), (61, 0))
            with self.assertRaises(ValueError):
                separator_analysis(doc.pages[0], mode='unknown')

    def test_cli_passes_explicit_mode(self):
        self.draw(lambda c: (self.line(c, 60), self.body(c)))
        out = self.root/'cli'
        rc = main([str(self.path), '--field', 'Require Key', '--query', 'XGen',
                   '--auto-separators', '--separator-mode', 'outermost', '--out', str(out)])
        report = json.loads((out/'extraction_report.json').read_text())
        self.assertEqual(report['files'][0]['page_cleaning'][0]['top_margin'], 61)
        self.assertEqual(rc, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
