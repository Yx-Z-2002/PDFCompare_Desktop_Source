"""Independent 2.1 geometric header/footer regressions using real PDFs.

Run: python tests/test_separator_service.py
"""
from __future__ import annotations
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'improved' / 'app'))
import pdfplumber
from reportlab.pdfgen import canvas
from margins import separator_analysis
from extract_requirements import extract_pdf, compile_query, effective_page_bbox
import service


def pdf(path, pages):
    c = canvas.Canvas(str(path), pagesize=(600,800))
    for ops in pages:
        c.setFont('Helvetica',11)
        for op in ops:
            if op[0]=='text': c.drawString(*op[1:])
            elif op[0]=='line': c.line(*op[1:])
            elif op[0]=='rect': c.rect(*op[1:5], stroke=0, fill=1)
            elif op[0]=='dash':
                c.setDash(4,3);c.line(*op[1:]);c.setDash()
            else: raise AssertionError(op)
        c.showPage()
    c.save()
    return path


def ordinary_page(header='Header 392 unique', footer='Footer 817 unique', top_y=750, bottom_y=60):
    return [('text',45,top_y+22,header), ('line',30,top_y,570,top_y),
            ('text',45,top_y-35,'AT-X-1: Safety requirement'),
            ('text',45,top_y-60,'The limit is 5 mm.'),
            ('text',45,top_y-85,'Require Key: XGen'),
            ('line',30,bottom_y,570,bottom_y),('text',45,bottom_y-25,footer)]


class SeparatorServiceRegression(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='pdfcompare-separator-')
        self.root=Path(self.tmp.name)
        self.ws=service.Workspace(self.root/'workspace')

    def tearDown(self):
        self.ws.close();self.tmp.cleanup()

    def analysis(self,path,number=0,**kwargs):
        with pdfplumber.open(path) as doc:
            page=doc.pages[number]
            return separator_analysis(page,bounds=effective_page_bbox(page),**kwargs)

    def extract(self,path,auto=True,**kwargs):
        return extract_pdf(path,compile_query('XGen'),field_name='Require Key',auto_separators=auto,**kwargs)

    def register(self,path):
        with path.open('rb') as f:
            return self.ws.register_document(f,path.stat().st_size,path.name)['id']

    def run_pair(self,a,b,auto=True):
        run=self.ws.extract(self.register(a),self.register(b),
            {'field':'Require Key','query':'XGen','auto_separators':auto,
             'output_root':str(self.root/'outputs'),'task_name':'separator_check'})
        result=self.ws.compare(run['id'],False)
        return run,result

    def test_single_page_nonrepeated_plain_text_is_cropped(self):
        path=pdf(self.root/'single.pdf',[ordinary_page()])
        analysis=self.analysis(path)
        self.assertGreater(analysis['top'],45);self.assertLess(analysis['top'],60)
        self.assertGreater(analysis['bottom'],55);self.assertLess(analysis['bottom'],70)
        report=self.extract(path)
        self.assertEqual(len(report['items']),1)
        self.assertTrue(report['items'][0]['matched'])
        self.assertEqual(report['items'][0]['description'],'The limit is 5 mm.')
        cleaning=report['page_cleaning'][0]
        self.assertEqual(cleaning['cropped_lines'],2)
        self.assertEqual(cleaning['auto_removed_lines'],2)
        self.assertEqual(cleaning['repeated_removed_lines'],0)
        self.assertEqual(cleaning['removed_lines'],2)
        audit=report['page_cleaning'][0]['auto_detection']
        removed=json.dumps(audit['removed_text'])
        self.assertIn('Header 392 unique',removed)
        self.assertIn('Footer 817 unique',removed)

    def test_disabled_auto_crop_retains_every_visible_region(self):
        path=pdf(self.root/'disabled.pdf',[ordinary_page()])
        report=self.extract(path,auto=False)
        cleaning=report['page_cleaning'][0]
        self.assertEqual(cleaning['top_margin'],0)
        self.assertEqual(cleaning['bottom_margin'],0)
        self.assertEqual(cleaning['crop_bbox'],cleaning['visible_bbox'])
        self.assertEqual(cleaning['removed_lines'],0)
        self.assertEqual(cleaning['cropped_lines'],0)
        self.assertEqual(cleaning['auto_removed_lines'],0)
        with pdfplumber.open(path) as doc:
            retained=doc.pages[0].crop(cleaning['crop_bbox']).extract_text()
        self.assertIn('Header 392 unique',retained)
        self.assertIn('Footer 817 unique',retained)

    def test_table_borders_near_page_edge_are_not_cropped(self):
        ops=[('line',30,770,570,770),('line',30,700,570,700),('line',30,640,570,640),
             ('line',30,770,30,640),('line',570,770,570,640),('line',250,770,250,640),
             ('text',45,745,'Important table content'),('text',45,680,'Require Key'),('text',270,680,'XGen')]
        path=pdf(self.root/'table.pdf',[ops])
        a=self.analysis(path)
        self.assertEqual((a['top'],a['bottom']),(0,0))
        self.assertTrue(any(not c['accepted'] for c in a['candidates']))

    def test_interior_horizontal_rule_does_not_crop_body(self):
        path=pdf(self.root/'interior.pdf',[[('text',45,740,'AT-X-1: Body'),('text',45,705,'Before the rule'),
             ('line',30,400,570,400),('text',45,380,'After the rule'),('text',45,350,'Require Key: XGen')]])
        a=self.analysis(path)
        self.assertEqual((a['top'],a['bottom']),(0,0))
        report=self.extract(path)
        self.assertIn('Before the rule',report['items'][0]['description'])
        self.assertIn('After the rule',report['items'][0]['description'])

    def test_nearest_outer_rule_wins_over_body_rule_in_edge_zone(self):
        path=pdf(self.root/'two-top.pdf',[[('text',45,777,'Actual header'),('line',30,750,570,750),
            ('text',45,710,'AT-X-1: Real requirement above second rule'),('line',30,665,570,665),
            ('text',45,645,'A real requirement description'),('text',45,620,'Require Key: XGen')]])
        a=self.analysis(path)
        self.assertGreater(a['top'],45);self.assertLess(a['top'],60)
        report=self.extract(path)
        self.assertEqual(len(report['items']),1)
        self.assertEqual(report['items'][0]['item_id'],'AT-X-1')
        self.assertTrue(report['items'][0]['matched'])

    def test_each_page_uses_its_own_rule_positions(self):
        path=pdf(self.root/'per-page.pdf',[ordinary_page(top_y=750,bottom_y=60),ordinary_page(top_y=710,bottom_y=95)])
        report=self.extract(path)
        cleaning=report['page_cleaning']
        self.assertEqual(len(cleaning),2)
        self.assertGreater(cleaning[1]['top_margin']-cleaning[0]['top_margin'],35)
        self.assertGreater(cleaning[1]['bottom_margin']-cleaning[0]['bottom_margin'],30)
        self.assertTrue(all(row['auto_detection']['removed_text']['top'] for row in cleaning))

    def test_thin_filled_rectangle_is_a_separator(self):
        ops=ordinary_page()
        ops[1]=('rect',30,749,540,1.5)
        path=pdf(self.root/'filled.pdf',[ops])
        a=self.analysis(path)
        self.assertGreater(a['top'],45);self.assertLess(a['top'],60)
        self.assertTrue(self.extract(path)['items'][0]['matched'])

    def test_text_crossing_rule_is_never_sliced(self):
        path=pdf(self.root/'crossing.pdf',[[('text',45,777,'Header candidate'),('line',30,750,570,750),
            ('text',45,748,'This text crosses the proposed separator.'),
            ('text',45,710,'AT-X-1: Requirement'),('text',45,680,'Require Key: XGen')]])
        a=self.analysis(path)
        self.assertEqual(a['top'],0)

    def test_no_text_outside_line_means_no_automatic_crop(self):
        path=pdf(self.root/'empty-outside.pdf',[[('line',30,750,570,750),('text',45,710,'AT-X-1: Body'),
            ('text',45,680,'Require Key: XGen')]])
        self.assertEqual(self.analysis(path)['top'],0)

    def test_full_service_comparison_ignores_nonrepeated_margin_changes(self):
        def pages(label):
            return [[('text',45,777,label+' top1'),('line',30,750,570,750),
                ('text',45,710,'AT-X-1: Cross-page requirement'),('text',45,680,'First body line.'),
                ('line',30,60,570,60),('text',45,30,label+' footer1')],
                [('text',45,777,label+' top2'),('line',30,750,570,750),
                 ('text',45,710,'Second body line.'),('text',45,680,'Require Key: XGen'),
                 ('line',30,60,570,60),('text',45,30,label+' footer2')]]
        a=pdf(self.root/'A.pdf',pages('Unique A'))
        b=pdf(self.root/'B.pdf',pages('Different B'))
        run,compared=self.run_pair(a,b,auto=True)
        self.assertEqual(compared['summary']['same'],1)
        self.assertEqual(compared['summary']['description_changed'],0)
        report=json.loads((Path(run['folder'])/'A'/'extraction_report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['items'][0]['description'],'First body line.\nSecond body line.')
        self.assertTrue(all('auto_detection' in row for row in report['page_cleaning']))
        _,off=self.run_pair(a,b,auto=False)
        self.assertEqual(off['summary']['description_changed'],1)

    def test_old_config_defaults_and_geometry_parameter_validation(self):
        base={'field':'Require Key','query':'XGen'}
        config=service.checked_config(base)
        self.assertEqual(config['separator_zone'],20)
        self.assertEqual(config['separator_min_width'],55)
        for key,values in {'separator_zone':(True,float('nan'),4,36),'separator_min_width':(True,float('nan'),29,96)}.items():
            for value in values:
                with self.subTest(key=key,value=value),self.assertRaises(ValueError):
                    service.checked_config({**base,key:value})
        for key,values in {'separator_zone':(5,20,35),'separator_min_width':(30,55,95)}.items():
            for value in values:
                with self.subTest(key=key,value=value):
                    self.assertEqual(service.checked_config({**base,key:value})[key],value)



    def test_manual_and_auto_crop_totals_count_each_line_once(self):
        ops=ordinary_page()
        ops.insert(2,('text',45,735,'Between automatic and manual boundaries'))
        path=pdf(self.root/'manual-and-auto.pdf',[ops])
        report=self.extract(path,auto=True,top_margin=80)
        cleaning=report['page_cleaning'][0]
        self.assertEqual(cleaning['cropped_lines'],3)
        self.assertEqual(cleaning['auto_removed_lines'],2)
        self.assertEqual(cleaning['repeated_removed_lines'],0)
        self.assertEqual(cleaning['removed_lines'],3)
        self.assertTrue(report['items'][0]['matched'])


if __name__=='__main__':unittest.main(verbosity=2)
