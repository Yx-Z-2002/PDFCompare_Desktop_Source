"""Native 2.1 page-edge preview integration and coordinate regression."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
from PIL import ImageGrab
from pypdf import PdfReader, PdfWriter

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'improved'/'app'))
sys.path.insert(0,str(ROOT/'tests'))
from test_separator_service import pdf,ordinary_page
import desktop


def main():
    passed=[];errors=[]
    with tempfile.TemporaryDirectory(prefix='pdfcompare-edge-gui-') as temp:
        base=Path(temp)
        original=pdf(base/'edge-pages.pdf',[ordinary_page(top_y=750,bottom_y=60),ordinary_page(top_y=710,bottom_y=95)])
        checksum=hashlib.sha256(original.read_bytes()).hexdigest()
        reader=PdfReader(original)
        page=reader.pages[0]
        page.cropbox.lower_left=(30,40);page.cropbox.upper_right=(570,780)
        writer=PdfWriter();writer.add_page(page)
        cropped=base/'cropbox.pdf'
        with cropped.open('wb') as stream:writer.write(stream)
        reader=PdfReader(original)
        page=reader.pages[0];page.rotate(180)
        writer=PdfWriter();writer.add_page(page)
        rotated=base/'rotated180.pdf'
        with rotated.open('wb') as stream:writer.write(stream)
        root=tk.Tk()
        app=desktop.DesktopApp(root,base/'state')
        root.report_callback_exception=lambda *args:errors.append(str(args))
        desktop.messagebox.showerror=lambda title,text,**kwargs:errors.append(text)
        desktop.messagebox.showinfo=lambda *args,**kwargs:None
        def pump(predicate,label,timeout=20):
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                root.update()
                if errors:raise AssertionError(errors)
                if predicate():return
                time.sleep(.015)
            raise AssertionError('Timeout '+label)
        def rendered(preview,label):
            pump(lambda:preview.last_analysis is not None and preview.label.get()!='正在渲染…',label)
        def screenshot(name):
            ImageGrab.grab(xdisplay=os.environ.get('DISPLAY')).save(ROOT/'tests'/name)
        def overlay_coordinates(preview):
            a=preview.last_analysis;bounds=a['visible_bbox'];iw,ih=preview.image_size
            sx,sy=iw/(bounds[2]-bounds[0]),ih/(bounds[3]-bounds[1])
            rectangles=[i for i in preview.canvas.find_withtag('page-edge-overlay') if preview.canvas.type(i)=='rectangle']
            expected=[a['preview_top']*sy,ih-a['preview_bottom']*sy]
            assert len(rectangles)==2
            coords=[preview.canvas.coords(i) for i in rectangles]
            assert abs(coords[0][3]-expected[0])<.01
            assert abs(coords[1][1]-expected[1])<.01
            for candidate in a['candidates']:
                if not candidate.get('accepted'):continue
                y=(candidate['y']-bounds[1])*sy
                lines=[i for i in preview.canvas.find_withtag('page-edge-overlay') if preview.canvas.type(i)=='line']
                assert any(abs(preview.canvas.coords(i)[1]-y)<.01 for i in lines)
            return sx,sy
        app.paths['A'].set(str(original));app.separator_side.set('A')
        assert not app.vars['field'].get() and not app.vars['query'].get()
        app.preview_separators();preview=app.preview_windows[-1]
        rendered(preview,'without query')
        assert preview.last_analysis['top']>0 and preview.last_analysis['bottom']>0
        assert preview.last_analysis['applied_top']==0 and preview.last_analysis['applied_bottom']==0
        assert '开关关闭' in preview.edge_summary.get()
        passed.append('preview_without_query_and_auto_off')
        before=len(preview.canvas.find_withtag('page-edge-overlay'))
        preview.show_mask.set(False);preview.draw_overlays()
        assert 0<len(preview.canvas.find_withtag('page-edge-overlay'))<before
        preview.show_lines.set(False);preview.draw_overlays()
        assert len(preview.canvas.find_withtag('page-edge-overlay'))==0
        preview.show_mask.set(True);preview.show_lines.set(True);preview.draw_overlays()
        passed.append('independent_overlay_toggles')
        app.vars['auto_separators'].set(True);preview.reload_settings();rendered(preview,'auto on')
        assert preview.last_analysis['applied_top']==preview.last_analysis['top']
        assert preview.last_analysis['applied_bottom']==preview.last_analysis['bottom']
        passed.append('auto_switch_updates_applied_crop')
        first_top=preview.last_analysis['top']
        preview.navigate(1);rendered(preview,'second page')
        assert preview.page==2 and preview.last_analysis['top']>first_top+35
        preview.page_var.set('1');preview.jump();rendered(preview,'jump first page')
        assert preview.page==1 and preview.last_analysis['top']==first_top
        passed.append('per_page_navigation_and_jump')
        preview.zoom(.65);rendered(preview,'zoom fit')
        overlay_coordinates(preview);preview.win.lift();root.update();screenshot('separator_overlay.png')
        preview.show_analysis();root.update()
        assert preview.analysis_window.winfo_exists()
        texts=[]
        def collect(widget):
            if isinstance(widget,tk.Text):texts.append(widget.get('1.0','end'))
            for child in widget.winfo_children():collect(child)
        collect(preview.analysis_window)
        assert any('Header 392 unique' in text and 'Footer 817 unique' in text for text in texts)
        screenshot('separator_evidence.png');preview.analysis_window.destroy()
        passed.append('overlay_coordinates_and_removed_text')
        preview.close()
        for path,label in [(cropped,'cropbox'),(rotated,'rotated180')]:
            app.paths['A'].set(str(path));app.preview_separators();p=app.preview_windows[-1]
            rendered(p,label)
            assert p.last_analysis['top']>0 and p.last_analysis['bottom']>0
            sx,sy=overlay_coordinates(p)
            assert abs(sx-sy)<.01
            if label=='cropbox':assert p.last_analysis['visible_bbox'][0]!=0 and p.last_analysis['visible_bbox'][1]!=0
            p.zoom(.65);rendered(p,label+' zoom');overlay_coordinates(p)
            p.win.lift();root.update();screenshot('separator_'+label+'.png');p.close()
            passed.append(label+'_overlay_mapping')
        app.paths['A'].set(str(original));app.preview_separators();p=app.preview_windows[-1]
        rendered(p,'manual merge')
        app.vars['top_margin_a'].set('100');p.reload_settings();rendered(p,'manual greater than auto')
        assert p.last_analysis['preview_top']==100 and p.last_analysis['applied_top']==100
        overlay_coordinates(p);p.close()
        passed.append('manual_crop_takes_maximum')
        app.vars['top_margin_a'].set('0')
        root.geometry('1100x700');root.update();screenshot('separator_main_small.png')
        app.advanced();root.update();app.advanced_window.geometry('800x500');root.update()
        screenshot('separator_advanced_small.png');app.advanced_window.destroy()
        passed.append('main_and_advanced_controls_render')
        assert checksum==hashlib.sha256(original.read_bytes()).hexdigest()
        passed.append('input_pdf_unchanged')
        app.close()
    result={'passed':passed,'count':len(passed),'errors':errors}
    (ROOT/'tests'/'separator_gui_report.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))

if __name__=='__main__':main()
