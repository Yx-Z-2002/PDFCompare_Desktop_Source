"""Real-PDF regression fixtures for repaired PDFCompare extraction."""
import sys, tempfile, unittest, re, os
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get('PDFCOMPARE_APP', str(ROOT / 'improved/app')))
from extract_requirements import extract_pdf, compile_query, requirement_heading, DEFAULT_ID_PATTERN, parse_field_line, make_field_patterns, Line
from margins import marginal_line
from reportlab.pdfgen.canvas import Canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
if Path(FONT).exists():
    pdfmetrics.registerFont(TTFont('Fixture', FONT))
    FONT_NAME = 'Fixture'
else:
    FONT_NAME = 'Helvetica'

class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'fixture.pdf'
    def tearDown(self): self.tmp.cleanup()
    def pdf(self, pages, duplicate=False):
        c = Canvas(str(self.path), pagesize=(600,800))
        for lines in pages:
            c.setFont(FONT_NAME,11)
            for i, line in enumerate(lines):
                x, txt = line if isinstance(line,tuple) else (50,line)
                c.setFont('STSong-Light' if any(ord(v)>0xff00 for v in txt) else FONT_NAME,11)
                c.drawString(x,750-i*20,txt)
                if duplicate: c.drawString(x+0.1,750-i*20,txt)
            c.showPage()
        c.save()
    def run_extract(self, **kwargs):
        return extract_pdf(self.path,compile_query('XGen'),field_name='Product Structure', **kwargs)
    def assert_one_match(self, report):
        self.assertEqual(len(report['items']),1,report)
        self.assertTrue(report['items'][0]['matched'],report)
    def test_normal_legacy(self):
        self.pdf([['AT-ABC-1 - Heading','Description','Product Structure: XGen']])
        self.assert_one_match(self.run_extract())
    def test_fullwidth_heading_and_label(self):
        self.pdf([['ＡＴ－ＡＢＣ－１：Heading','Product Structure：XGen']])
        self.assert_one_match(self.run_extract())
    def test_letter_spaced_label(self):
        self.pdf([['AT-ABC-1 - Heading','P r o d u c t S t r u c t u r e : XGen']])
        self.assert_one_match(self.run_extract())
    def test_bare_id_wrapped_heading(self):
        self.pdf([['AT-ABC-1','A complete heading','Description','Product Structure: XGen']])
        r=self.run_extract();self.assert_one_match(r)
        self.assertEqual(r['items'][0]['title'],'A complete heading')
    def test_wrapped_field_label(self):
        self.pdf([['AT-ABC-1 - Heading','Description','Product','Structure: XGen']])
        self.assert_one_match(self.run_extract())
    def test_label_only_then_value(self):
        self.pdf([['AT-ABC-1 - Heading','Product Structure','XGen','Test Level: Unit']])
        r=self.run_extract();self.assert_one_match(r)
        self.assertEqual(r['items'][0]['field_value'],'XGen')
    def test_no_false_match_next_unknown_field(self):
        self.pdf([['AT-ABC-1 - Heading','Product Structure', 'Custom Category: XGen']])
        r=self.run_extract();self.assertFalse(r['items'][0]['matched'])
    def test_four_column_table_is_separated(self):
        c=Canvas(str(self.path),pagesize=(600,800));c.setFont('Helvetica',10)
        c.drawString(40,750,'AT-ABC-1 - Heading')
        xs=[40,180,290,430,550]; ys=[700,670,640]
        for x in xs: c.line(x,640,x,700)
        for y in ys: c.line(40,y,550,y)
        for x,t in zip(xs,['Product Structure','Alpha','Test Level','XGen']):
            c.drawString(x+3,680,t)
        for x,t in zip(xs,['','Beta','','Unit']):
            c.drawString(x+3,650,t)
        c.showPage();c.save()
        r=self.run_extract()
        self.assertEqual(len(r['items']),1)
        self.assertFalse(r['items'][0]['matched'],r)
        self.assertEqual(r['items'][0]['field_value'],'Alpha\nBeta')
    def test_field_after_number_column(self):
        c=Canvas(str(self.path),pagesize=(600,800));c.setFont('Helvetica',10)
        c.drawString(40,750,'AT-ABC-1 - Heading')
        xs=[40,80,280,550];ys=[700,670,640]
        for x in xs:c.line(x,640,x,700)
        for y in ys:c.line(40,y,550,y)
        for x,t in zip(xs,['1','Product Structure','XGen']):c.drawString(x+3,680,t)
        for x,t in zip(xs,['2','Test Level','Unit']):c.drawString(x+3,650,t)
        c.showPage();c.save()
        self.assert_one_match(self.run_extract())
    def test_no_false_match_next_known_field(self):
        self.pdf([['AT-ABC-1 - Heading','Product Structure','Test Level: XGen']])
        r=self.run_extract();self.assertFalse(r['items'][0]['matched'])
    def test_missing_text_page_breaks_ownership(self):
        self.pdf([['AT-ABC-1 - Heading','Previous description'],[],['Product Structure: XGen']])
        r=self.run_extract();self.assertFalse(r['items'][0]['matched'])
        self.assertEqual(r['diagnostics'][1]['status'],'no_text')
        self.assertTrue(any('没有对应标题' in w for w in r['warnings']))
    def test_overprinted_glyphs_deduplicated(self):
        self.pdf([['AT-ABC-1 - Heading','Product Structure: XGen']],duplicate=True)
        self.assert_one_match(self.run_extract())
    def test_literal_scientific_unicode_preserved(self):
        self.pdf([['AT–ABC–1: Energy m² α','Product Structure: XGen']])
        r=self.run_extract();self.assert_one_match(r)
        self.assertEqual(r['items'][0]['title'],'Energy m² α')
    def test_diagnostic_candidates_and_text(self):
        self.pdf([['AT-ABC-1 - Heading','Product Structure: XGen']])
        d=self.run_extract()['diagnostics'][0]
        self.assertEqual(d['candidate_ids'],['AT-ABC-1'])
        self.assertIn('Product Structure',d['text_preview'])
    def test_margins_protect_unicode_heading(self):
        line=Line('AT–ABC–1 - Heading',(20,5,200,15))
        pattern=re.compile(r'^\s*(?P<id>'+DEFAULT_ID_PATTERN+r')(?!\w)',re.I)
        keys={('top',line.text.casefold())}
        self.assertFalse(marginal_line(line,800,keys,.1,pattern))
    def test_id_title_space_separator(self):
        self.pdf([['AT-ABC-1 Title without separator','Product Structure: XGen']])
        self.assert_one_match(self.run_extract())
    def test_identifier_split_across_lines(self):
        self.pdf([['AT-ABC-','123 - Title','Product Structure: XGen']])
        r=self.run_extract();self.assert_one_match(r)
        self.assertEqual(r['items'][0]['item_id'],'AT-ABC-123')

if __name__=='__main__': unittest.main(verbosity=2)
