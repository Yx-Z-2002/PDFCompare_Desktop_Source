"""Drive native Tk widgets and real background service work under a display.
Set DISPLAY to a disposable Xvfb (or native desktop) before running.
"""
from __future__ import annotations
import csv
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
from PIL import ImageGrab

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'improved' / 'app'))
sys.path.insert(0, str(ROOT / 'tests'))
from regression_service import make_pdf, block
import desktop


def main():
    passed = []
    errors = []
    with tempfile.TemporaryDirectory(prefix='pdfcompare-gui-') as temp:
        base = Path(temp)
        a = make_pdf(base/'A.pdf', [block() + block('AT-X-2', 'Changed requirement', 'Distance is 7 mm.', y=595) + block('AT-X-3', 'Filtered requirement', y=455)])
        b = make_pdf(base/'B.pdf', [block() + block('AT-X-2', 'Changed requirement', 'Distance is 8 mm.', y=595) + block('AT-X-3', 'Filtered requirement', value='Other', y=455)])
        new_b = make_pdf(base/'NewB.pdf', [[('text',45,735,'Current file search marker ZEBRA42')]])
        root = tk.Tk()
        root.report_callback_exception = lambda *args: errors.append(str(args))
        desktop.messagebox.showerror = lambda title, text, **kwargs: errors.append(text)
        desktop.messagebox.showinfo = lambda *args, **kwargs: None
        desktop.messagebox.askyesno = lambda *args, **kwargs: True
        app = desktop.DesktopApp(root, base/'state')
        def pump_until(predicate, label, timeout=20):
            deadline = time.monotonic()+timeout
            while time.monotonic()<deadline:
                root.update()
                if predicate():
                    if errors: raise AssertionError(errors)
                    return
                time.sleep(.015)
            raise AssertionError('Timed out: '+label+'; status='+app.status.get())
        root.update()
        assert root.winfo_width() >= 980
        passed.append('native_window_constructs')
        picks = iter([str(a), str(b)])
        desktop.filedialog.askopenfilename = lambda **kwargs: next(picks)
        app.choose_pdf('A'); app.choose_pdf('B')
        assert app.paths['A'].get() == str(a) and app.paths['B'].get() == str(b)
        app.vars['field'].set('Require Key'); app.vars['query'].set('XGen')
        app.vars['output_root'].set(str(base/'output'))
        app.vars['task_name'].set('GUI regression')
        app.start(True)
        assert app.busy
        pump_until(lambda:not app.busy, 'extract and compare')
        assert len(app.tables['A'].items)==3
        assert len(app.tables['B'].items)==3
        assert [x.status for x in app.tables['comparison'].items] == ['same','description_changed','only_a']
        assert '命中 3' in app.summary.get() and '命中 2' in app.summary.get()
        passed.append('background_extract_and_compare')
        assert not app.inputs_visible, 'Completed comparison should leave room for colored details'
        table = app.tables['B']
        table.filter.set('未命中'); root.update()
        assert len(table.tree.get_children())==1
        table.tree.selection_set(table.tree.get_children()[0]); table.selected()
        assert 'Other' in table.details.get('1.0','end')
        table.filter.set('全部'); table.search.set('Changed'); root.update()
        assert len(table.tree.get_children())==1
        passed.append('result_filter_search_and_detail')
        comp = app.tables['comparison']
        comp.tree.selection_set('2'); comp.selected()
        assert '未满足本次查询条件' in comp.details.get('1.0','end')
        passed.append('comparison_audit_visible')
        comp.show_details(); root.update()
        assert comp.detail_window.winfo_exists()
        ImageGrab.grab(xdisplay=os.environ.get('DISPLAY')).save(ROOT/'tests'/'gui_full_details.png')
        comp.detail_window.destroy()
        passed.append('expanded_detail_window')
        app.tabs.select(comp.frame); root.update()
        ImageGrab.grab(xdisplay=os.environ.get('DISPLAY')).save(ROOT/'tests'/'gui_results.png')
        root.geometry('1100x700'); root.update()
        descendants = []
        def collect(widget):
            descendants.append(widget)
            for child in widget.winfo_children(): collect(child)
        collect(root)
        progress_bars = [w for w in descendants if isinstance(w, desktop.ttk.Progressbar)]
        assert progress_bars and progress_bars[0].winfo_ismapped()
        assert progress_bars[0].winfo_rooty() + progress_bars[0].winfo_height() <= root.winfo_rooty() + root.winfo_height()
        ImageGrab.grab(xdisplay=os.environ.get('DISPLAY')).save(ROOT/'tests'/'gui_small.png')
        root.geometry('1220x880'); root.update()
        passed.append('small_window_retains_footer')
        target = base/'export.csv'
        desktop.filedialog.asksaveasfilename = lambda **kwargs: str(target)
        app.export_table()
        with target.open(encoding='utf-8-sig') as f: rows = list(csv.DictReader(f))
        assert len(rows)==3 and any('未满足本次查询条件' in row['备注'] for row in rows)
        passed.append('csv_export_contains_audit')
        app.locate_text.set('AT-X-3'); app.locate_side.set('B'); app.locate()
        pump_until(lambda:not app.busy, 'source search')
        assert 'AT-X-3' in app.diagnostics.get('1.0','end')
        assert '第 1 页' in app.diagnostics.get('1.0','end')
        passed.append('original_text_search')
        app.paths['B'].set(str(new_b)); app.locate_text.set('ZEBRA42'); app.locate()
        pump_until(lambda:not app.busy, 'new file source search')
        assert 'ZEBRA42' in app.diagnostics.get('1.0','end')
        assert '找到 1' in app.diagnostics.get('1.0','end')
        passed.append('source_search_uses_newly_selected_file')
        app.preview('A',1)
        preview = app.preview_windows[-1]
        pump_until(lambda:hasattr(preview,'photo'), 'native PDF preview')
        assert preview.total == 1
        preview.zoom(1.25)
        pump_until(lambda:preview.label.get() != '正在渲染…', 'preview zoom')
        ImageGrab.grab(xdisplay=os.environ.get('DISPLAY')).save(ROOT/'tests'/'gui_preview.png')
        preview.win.destroy()
        passed.append('native_pdf_preview_and_zoom')
        app.paths['B'].set(str(b))
        app.start(False)
        pump_until(lambda:not app.busy, 'extract only')
        assert app.tables['comparison'].items == []
        app.recompare(); pump_until(lambda:not app.busy, 'recompare')
        assert len(app.tables['comparison'].items)==3
        passed.append('extract_only_then_recompare')
        app.advanced(); root.update()
        ImageGrab.grab(xdisplay=os.environ.get('DISPLAY')).save(ROOT/'tests'/'gui_advanced.png')
        assert app.advanced_window.winfo_exists()
        app.advanced_window.geometry('800x500'); root.update()
        ImageGrab.grab(xdisplay=os.environ.get('DISPLAY')).save(ROOT/'tests'/'gui_advanced_small.png')
        buttons=[]
        def advanced_buttons(widget):
            if isinstance(widget,desktop.ttk.Button): buttons.append(widget)
            for child in widget.winfo_children(): advanced_buttons(child)
        advanced_buttons(app.advanced_window)
        close_button=next(w for w in buttons if w.cget('text')=='关闭')
        assert close_button.winfo_ismapped()
        assert close_button.winfo_rooty()+close_button.winfo_height()<=app.advanced_window.winfo_rooty()+app.advanced_window.winfo_height()
        passed.append('advanced_footer_visible_at_500px')
        app.advanced_window.destroy()
        passed.append('advanced_settings_dialog')
        app.close()
    result = {'passed':passed,'count':len(passed),'errors':errors}
    (ROOT/'tests'/'gui_test_report.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
