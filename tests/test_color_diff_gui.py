"""Native widget tags, mode controls and expanded details; requires DISPLAY."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
import unittest
from PIL import ImageGrab

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'improved'/'app'))
import desktop
import compare_matches as c


class ColorGUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='pdfcompare-color-')
        cls.root=tk.Tk()
        cls.app=desktop.DesktopApp(cls.root,Path(cls.temp.name)/'state')
        cls.root.update()
        cls.table=cls.app.tables['comparison']
        cls.app.tabs.select(cls.table.frame)
        cls.errors=[]
        desktop.messagebox.showerror=lambda *a,**k:cls.errors.append(str(a))
        desktop.messagebox.askyesno=lambda *a,**k:True

    @classmethod
    def tearDownClass(cls):
        cls.app.close()
        cls.temp.cleanup()

    def setUp(self):
        self.table.filter.set('全部'); self.table.search.set('')
        self.app.audit={}

    def load(self,a,b,ignore=False):
        items=c.compare(c.CsvData(Path('a.csv'),'utf-8',a),c.CsvData(Path('b.csv'),'utf-8',b),ignore_whitespace=ignore)
        self.table.load(items)
        self.table.tree.selection_set('0'); self.table.selected()
        self.root.update()
        return items[0]

    @staticmethod
    def tagged(widget,tag):
        ranges=widget.tag_ranges(tag)
        return [widget.get(a,b) for a,b in zip(ranges[::2],ranges[1::2])]

    def test_changed_text_colors_and_full_dialog(self):
        self.load({'X':[c.Record('X','安全<标题>','距离为7毫米，保持稳定。',2)]},
                  {'X':[c.Record('X','安全<标题>','距离为8毫米，保持稳定。',2)]})
        self.assertEqual(self.tagged(self.table.details,'delete'),['7'])
        self.assertEqual(self.tagged(self.table.details,'insert'),['8'])
        self.assertIn('安全<标题>',self.table.details.get('1.0','end'))
        self.table.show_details(); self.root.update()
        self.assertEqual(self.tagged(self.table.detail_text,'delete'),['7'])
        self.assertEqual(self.tagged(self.table.detail_text,'insert'),['8'])
        self.assertEqual(self.table.detail_text.get('1.0','end'),self.table.details.get('1.0','end'))
        ImageGrab.grab(xdisplay=os.environ['DISPLAY']).save(ROOT/'tests'/'color_diff_full_details.png')
        self.table.detail_window.destroy()

    def test_both_title_and_description_colors(self):
        self.load({'X':[c.Record('X','原标题','7 mm',2)]},{'X':[c.Record('X','新标题','8 mm',2)]})
        self.assertEqual(self.tagged(self.table.details,'delete'),['原','7'])
        self.assertEqual(self.tagged(self.table.details,'insert'),['新','8'])
        ImageGrab.grab(xdisplay=os.environ['DISPLAY']).save(ROOT/'tests'/'color_diff_results.png')

    def test_only_a_b_colors_and_audit(self):
        for side,color in [('a','delete'),('b','insert')]:
            with self.subTest(side=side):
                records={'X':[c.Record('X','单侧标题','单侧描述',2)]}
                self.app.audit={'X':{'reason':'未满足本次查询条件','field_values':['Other']}}
                self.load(records if side=='a' else {},records if side=='b' else {})
                self.assertEqual(self.tagged(self.table.details,color),['单侧标题','单侧描述'])
                self.assertIn('only_'+side,self.table.tree.item('0','tags'))
                self.assertIn('当前提取命中结果',self.table.details.get('1.0','end'))

    def test_duplicates_not_falsely_paired(self):
        self.load({'X':[c.Record('X','甲','描述甲',2),c.Record('X','乙','描述乙',3)]},
                  {'X':[c.Record('X','丙','描述丙',2)]})
        self.assertEqual(self.tagged(self.table.details,'delete'),[])
        self.assertEqual(self.tagged(self.table.details,'insert'),[])
        self.assertIn('未自动配对',self.table.details.get('1.0','end'))
        self.assertIn('CSV 第 3 行',self.table.details.get('1.0','end'))

    def test_ignored_whitespace_no_content_color(self):
        self.load({'X':[c.Record('X','标题','a  b\nc',2)]},{'X':[c.Record('X','标题','a b c',2)]},True)
        self.assertEqual(self.tagged(self.table.details,'delete'),[])
        self.assertEqual(self.tagged(self.table.details,'insert'),[])
        self.assertIn('空白排版差异',self.table.details.get('1.0','end'))

    def test_refresh_clears_old_color(self):
        self.load({'X':[c.Record('X','原','甲',2)]},{'X':[c.Record('X','新','乙',2)]})
        self.table.search.set('不存在')
        self.root.update()
        self.assertEqual(self.tagged(self.table.details,'delete'),[])
        self.assertEqual(self.tagged(self.table.details,'insert'),[])
        self.assertIn('没有条目',self.table.details.get('1.0','end'))

    def test_mode_default_and_two_way_binding(self):
        self.assertEqual(desktop.DEFAULTS['separator_mode'],'outermost')
        self.app.vars['separator_mode'].set('conservative')
        self.assertEqual(self.app.separator_mode_label.get(),desktop.SEPARATOR_MODES['conservative'])
        self.app.separator_mode_label.set(desktop.SEPARATOR_MODES['outermost'])
        self.app.select_separator_mode()
        self.assertEqual(self.app.vars['separator_mode'].get(),'outermost')
        self.assertEqual(self.app.separator_preview_config('A')['separator_mode'],'outermost')
        self.app.advanced(); self.root.update()
        self.app.vars['separator_mode'].set('conservative')
        self.assertEqual(self.app.separator_mode_label.get(),desktop.SEPARATOR_MODES['conservative'])
        self.app.advanced_window.destroy()
        self.app.vars['separator_mode'].set('outermost')

    def test_outermost_preview_and_combined_crop_guard(self):
        from reportlab.pdfgen import canvas
        path=Path(self.temp.name)/'outer-border.pdf'
        pdf=canvas.Canvas(str(path),pagesize=(600,800))
        pdf.setFont('Helvetica',11)
        pdf.drawString(45,770,'Header'); pdf.drawString(45,30,'Footer')
        pdf.drawString(45,600,'Body text')
        pdf.rect(40,60,520,690,stroke=1,fill=0)
        pdf.save()
        def wait(predicate):
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                self.root.update()
                if predicate():return
                time.sleep(.02)
            self.fail('Timed out waiting for PDF preview: '+repr(self.errors))
        self.app.paths['A'].set(str(path))
        self.app.vars['separator_mode'].set('outermost')
        self.app.vars['auto_separators'].set(True)
        self.app.preview_separators(); preview=self.app.preview_windows[-1]
        try:
            wait(lambda:preview.last_analysis is not None)
            self.assertEqual(preview.last_analysis['mode'],'outermost')
            self.assertGreater(preview.last_analysis['top'],49)
            self.assertGreater(preview.last_analysis['bottom'],59)
            self.assertEqual(preview.last_analysis['applied_top'],preview.last_analysis['top'])
            self.assertIn('顶部第一条',preview.edge_summary.get())
            self.app.vars['top_margin_a'].set('750')
            self.errors.clear(); preview.reload_settings()
            wait(lambda:bool(self.errors))
            self.assertIn('合并后覆盖整页',self.errors[-1])
            self.errors.clear()
            self.app.vars['auto_separators'].set(False)
            preview.reload_settings()
            wait(lambda:preview.label.get()!='正在渲染…')
            self.assertTrue(any('重叠' in w for w in preview.last_analysis['warnings']))
            self.assertEqual(preview.last_analysis['applied_top'],750)
            self.assertEqual(preview.last_analysis['applied_bottom'],0)
        finally:
            preview.close()
            self.app.vars['top_margin_a'].set('0')
            self.app.vars['auto_separators'].set(False)

    def test_small_main_keeps_selector_visible(self):
        self.root.geometry('980x660'); self.root.update()
        box=self.app.separator_mode_box
        self.assertTrue(box.winfo_ismapped())
        self.assertGreaterEqual(box.winfo_rootx(),self.root.winfo_rootx())
        self.assertLessEqual(box.winfo_rootx()+box.winfo_width(),self.root.winfo_rootx()+self.root.winfo_width())
        label_font=desktop.tkfont.Font(font=desktop.ttk.Style().lookup('TCombobox','font'))
        self.assertGreaterEqual(box.winfo_width(),label_font.measure(box.get())+20)
        ImageGrab.grab(xdisplay=os.environ['DISPLAY']).save(ROOT/'tests'/'color_diff_small.png')
        self.root.geometry('1220x880'); self.root.update()


if __name__=='__main__': unittest.main(verbosity=2)
