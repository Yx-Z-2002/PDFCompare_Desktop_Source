"""Real PDF fixtures for geometry-based header/footer detection (v2.1)."""
import sys, unittest, tempfile
from pathlib import Path
from reportlab.pdfgen.canvas import Canvas
from pypdf import PdfReader, PdfWriter
import pdfplumber
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'improved/app'))
from margins import separator_analysis, separator_bounds
from extract_requirements import effective_page_bbox, extract_pdf, compile_query

class SeparatorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'input.pdf'
    def tearDown(self):self.temp.cleanup()
    def draw(self, callback):
        canvas=Canvas(str(self.path),pagesize=(600,800));canvas.setFont('Helvetica',11)
        callback(canvas);canvas.save()
    def text(self,c,top,text,x=50):c.drawString(x,800-top,text)
    def line(self,c,top,dashed=False,x0=40,x1=560):
        c.setDash(4,3) if dashed else c.setDash()
        c.line(x0,800-top,x1,800-top);c.setDash()
    def analysis(self,n=0,**kwargs):
        with pdfplumber.open(self.path) as pdf:
            return separator_analysis(pdf.pages[n],bounds=effective_page_bbox(pdf.pages[n]),**kwargs)
    def ordinary(self,c):
        self.text(c,38,'Ordinary narrative in the header.')
        self.line(c,62)
        self.text(c,110,'AT-ABC-1 - Body requirement')
        self.text(c,135,'Product Structure: XGen')
        self.line(c,737)
        self.text(c,770,'Ordinary narrative in the footer.')
    def test_single_page_solid_header_and_footer(self):
        self.draw(self.ordinary);r=self.analysis()
        self.assertAlmostEqual(r['top'],63,places=1)
        self.assertAlmostEqual(r['bottom'],64,places=1)
        self.assertIn('Ordinary narrative in the header.',' '.join(r['removed_text']['top']))
        self.assertEqual(sum(c['accepted'] for c in r['candidates']),2)
    def test_page_specific_text_and_line_position(self):
        def draw(c):
            for i in range(3):
                self.text(c,35,'Different header '+str(i));self.line(c,60+i*9)
                self.text(c,140,'AT-ABC-'+str(i+1)+' - Body')
                self.text(c,165,'Product Structure: XGen');c.showPage();c.setFont('Helvetica',11)
        self.draw(draw)
        self.assertEqual([self.analysis(i)['top'] for i in range(3)],[61,70,79])
    def test_dashed_header(self):
        def draw(c):self.text(c,35,'Header');self.line(c,65,True);self.text(c,130,'Body')
        self.draw(draw);r=self.analysis();self.assertEqual(r['top'],66)
        self.assertEqual(r['candidates'][0]['kind'],'dashed_line')
    def test_thin_filled_rectangle(self):
        def draw(c):
            self.text(c,35,'Header');c.rect(40,735,520,2,stroke=0,fill=1);self.text(c,120,'Body')
        self.draw(draw);r=self.analysis();self.assertGreater(r['top'],64)
        chosen=[v for v in r['candidates'] if v['accepted']]
        self.assertEqual(len(chosen),1);self.assertEqual(chosen[0]['kind'],'filled_rectangle')
    def test_vertical_border_rejects_table(self):
        def draw(c):
            self.text(c,35,'Text above table')
            for y in [60,95,130]:self.line(c,y)
            for x in [40,260,560]:c.line(x,740,x,670)
            self.text(c,85,'Cell value');self.text(c,120,'Another cell')
        self.draw(draw);r=self.analysis();self.assertEqual(r['top'],0)
        self.assertTrue(any('竖线' in v['reason'] for v in r['candidates']))
    def test_internal_vertical_divider_also_rejects_table(self):
        def draw(c):
            self.text(c,35,'Top cell');self.line(c,65);c.line(300,750,300,710)
            self.text(c,110,'Body')
        self.draw(draw);self.assertEqual(self.analysis()['top'],0)
    def test_horizontal_only_grid_rejected(self):
        def draw(c):
            self.text(c,35,'Text')
            for y in [60,90,120]:self.line(c,y)
            self.text(c,82,'Row 1');self.text(c,112,'Row 2')
        self.draw(draw);r=self.analysis();self.assertEqual(r['top'],0)
        self.assertTrue(any('等距' in v['reason'] for v in r['candidates']))
    def test_central_body_rule_rejected(self):
        def draw(c):self.text(c,360,'Paragraph');self.line(c,400);self.text(c,440,'Next paragraph')
        self.draw(draw);r=self.analysis();self.assertEqual((r['top'],r['bottom']),(0,0))
    def test_nearest_of_two_top_lines_preserves_body(self):
        def draw(c):
            self.text(c,30,'Header');self.line(c,55);self.text(c,95,'AT-ABC-1 - Real body')
            self.line(c,130);self.text(c,155,'Product Structure: XGen')
        self.draw(draw);r=self.analysis();self.assertEqual(r['top'],56)
        self.assertNotIn('Real body',' '.join(r['removed_text']['top']))
        self.assertTrue(any('更靠近' in v['reason'] for v in r['candidates']))
    def test_text_crossing_rule_preserved(self):
        def draw(c):self.text(c,30,'Header');self.text(c,68,'Text crossing');self.line(c,65)
        self.draw(draw);self.assertEqual(self.analysis()['top'],0)
    def test_no_outer_text_insufficient_evidence(self):
        def draw(c):self.line(c,60);self.text(c,110,'Body')
        self.draw(draw);self.assertEqual(self.analysis()['top'],0)
    def test_real_requirement_words_in_header_are_removed_by_geometry(self):
        def draw(c):
            self.text(c,26,'AT-X-900 - Header requirement-like words')
            self.text(c,45,'Product Structure: XGen');self.line(c,65)
            self.text(c,110,'AT-X-1 - Actual body');self.text(c,135,'Product Structure: XGen')
        self.draw(draw)
        r=extract_pdf(self.path,compile_query('XGen'),field_name='Product Structure',auto_separators=True)
        self.assertEqual([v['item_id'] for v in r['items']],['AT-X-1'])
        self.assertIn('AT-X-900',' '.join(r['page_cleaning'][0]['auto_detection']['removed_text']['top']))
    def test_cropbox_relative_margins_and_bounds(self):
        def draw(c):
            self.text(c,75,'Visible header');self.line(c,100);self.text(c,160,'Body')
            self.line(c,700);self.text(c,735,'Visible footer')
        self.draw(draw)
        reader=PdfReader(self.path);page=reader.pages[0];page.cropbox.lower_left=(20,40);page.cropbox.upper_right=(580,760)
        writer=PdfWriter();writer.add_page(page);temp=self.path.with_name('cropped.pdf');writer.write(temp);temp.replace(self.path)
        r=self.analysis();self.assertEqual(r['visible_bbox'],[20,40,580,760])
        self.assertEqual(r['top'],61);self.assertEqual(r['bottom'],61)
    def test_boundary_rule_does_not_make_negative_crop(self):
        def draw(c):self.line(c,0);self.line(c,800);self.text(c,400,'Body')
        self.draw(draw);r=self.analysis();self.assertEqual((r['top'],r['bottom']),(0,0))
    def test_rotated_180_page_detects_other_edge(self):
        def draw(c):self.text(c,30,'Header');self.line(c,65);self.text(c,120,'Body')
        self.draw(draw)
        reader=PdfReader(self.path);page=reader.pages[0];page.rotate(180)
        writer=PdfWriter();writer.add_page(page);temp=self.path.with_name('rotated.pdf');writer.write(temp);temp.replace(self.path)
        r=self.analysis();self.assertEqual(r['top'],0);self.assertAlmostEqual(r['bottom'],66,places=1)
    def test_no_text_page_has_cleaning_record(self):
        self.draw(lambda c:(self.line(c,65),c.showPage()))
        r=extract_pdf(self.path,compile_query('XGen'),field_name='Product Structure',auto_separators=True)
        self.assertEqual(len(r['page_cleaning']),1)
        self.assertIn('auto_detection',r['page_cleaning'][0])
    def test_repeated_cleanup_uses_same_nearest_boundary(self):
        def draw(c):
            for i in range(2):
                c.setFont('Helvetica',10)
                self.text(c,25,'Unique header '+str(i));self.line(c,45)
                self.text(c,60,'AT-ABC-'+str(i+1)+' - Body')
                self.text(c,75,'Important repeated requirement text');self.line(c,90)
                self.text(c,120,'Product Structure: XGen');c.showPage()
        self.draw(draw)
        r=extract_pdf(self.path,compile_query('XGen'),field_name='Product Structure',
                      auto_separators=True,remove_repeated=True)
        self.assertEqual(len(r['items']),2)
        for item in r['items']:
            self.assertIn('Important repeated requirement text',item['description'])
        for p in r['page_cleaning']:
            self.assertEqual(p['cropped_lines'],1)
            self.assertEqual(p['repeated_removed_lines'],0)
    def test_compatibility_wrapper_and_adjustable_limits(self):
        def draw(c):self.text(c,30,'Header');self.line(c,120,x0=120,x1=480);self.text(c,180,'Body')
        self.draw(draw)
        self.assertEqual(self.analysis(zone=10)['top'],0)
        self.assertEqual(self.analysis(min_width=70)['top'],0)
        with pdfplumber.open(self.path) as pdf:self.assertEqual(separator_bounds(pdf.pages[0]),(121,0))

if __name__=='__main__':unittest.main(verbosity=2)
