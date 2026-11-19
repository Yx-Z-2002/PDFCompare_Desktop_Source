"""2.3 native cancellation workflows; run under a real Tk display/Xvfb."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from PIL import ImageGrab

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'improved/app'))
sys.path.insert(0,str(ROOT/'tests'))
from regression_service import make_pdf,block
import desktop
from task_control import TaskCancelled


class CancellationGUITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pdfcompare-cancel-gui-')
        self.base=Path(self.temp.name)
        self.root=tk.Tk()
        self.app=desktop.DesktopApp(self.root,self.base/'state')
        self.errors=[]
        self.root.report_callback_exception=lambda *args:self.errors.append(str(args))
        desktop.messagebox.showerror=lambda *args,**kwargs:self.errors.append(str(args))
        self.a=make_pdf(self.base/'A.pdf',[block()])
        self.b=make_pdf(self.base/'B.pdf',[block(desc='The limit is 6 mm.')])
        self.app.paths['A'].set(str(self.a));self.app.paths['B'].set(str(self.b))
        self.app.vars['field'].set('Require Key');self.app.vars['query'].set('XGen')
        self.app.vars['output_root'].set(str(self.base/'output'))
        self.app.vars['task_name'].set('Cancel regression')
        self.releases=[]
        self.root.update()

    def tearDown(self):
        for event in self.releases:event.set()
        if not self.app.closed:
            self.app.close()
            deadline=time.monotonic()+10
            while not self.app.closed and time.monotonic()<deadline:
                self.root.update();time.sleep(.01)
            self.assertTrue(self.app.closed,'worker did not finish before close')
        self.temp.cleanup()

    def pump(self,predicate,timeout=25):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if not self.app.closed:self.root.update()
            if self.errors:raise AssertionError(self.errors)
            if predicate():return
            time.sleep(.01)
        self.fail('Timed out: '+self.app.status.get())

    def gate_method(self,name):
        original=getattr(self.app.workspace,name)
        entered=threading.Event();release=threading.Event();self.releases.append(release)
        def gated(*args,**kwargs):
            entered.set()
            if not release.wait(15):raise RuntimeError('Test gate timed out')
            return original(*args,**kwargs)
        setattr(self.app.workspace,name,gated)
        return original,entered,release

    def complete(self):
        self.app.start(True)
        self.pump(lambda:not self.app.busy)
        self.assertIsNotNone(self.app.run_result)
        self.assertFalse(self.app.inputs_visible)
        return self.app.run_result

    def assert_stopping(self):
        self.app.stop_btn.invoke()
        self.assertTrue(self.app.busy)
        self.assertIn('正在中止',self.app.status.get())
        self.assertEqual(str(self.app.stop_btn['state']),'disabled')
        self.assertEqual(str(self.app.start_btn['state']),'disabled')
        self.app.stop_task()  # idempotent
        heartbeat=[]
        self.root.after(1,lambda:heartbeat.append(True))
        self.pump(lambda:bool(heartbeat))

    def test_idle_stop_disabled_and_missing_label_is_uncertain(self):
        self.assertEqual(str(self.app.stop_btn['state']),'disabled')
        self.app.stop_task();self.assertFalse(self.app.busy)
        item={'item_id':'AT-X-9','field_status':'missing','title':'Visible source',
              'field_value':'','description':'','pages':[1],'notes':['尚未可靠读取字段'],
              'attributes':{},'query_field':'Require Key','matched':False,
              'field_diagnostic':{'summary':'页边区域出现字段字样，需核对裁切范围。',
                 'evidence':[{'page':1,'text':'Require Key: XGen','disposition':'cropped'}]}}
        self.assertEqual(desktop.ResultTable.status(item),'字段未提取到')
        table=self.app.tables['A'];table.load([item]);table.filter.set('字段未提取到 / 为空')
        self.assertEqual(len(table.tree.get_children()),1)
        table.tree.selection_set('0');table.selected()
        self.assertIn('尚未可靠读取字段',table.details.get('1.0','end'))
        self.assertIn('字段提取排查',table.details.get('1.0','end'))
        self.assertIn('第 1 页 · 被页边裁切排除',table.details.get('1.0','end'))
        self.assertIn('Require Key: XGen',table.details.get('1.0','end'))
        table.filter.set('字段缺失 / 为空')
        self.assertEqual(len(table.tree.get_children()),1)

    def test_cancel_running_extraction_keeps_prior_result_and_can_restart(self):
        old=self.complete();old_items=list(self.app.tables['comparison'].items)
        original,entered,release=self.gate_method('extract')
        self.app.start(True);self.pump(entered.is_set)
        self.assert_stopping()
        token=self.app.job_id
        self.app.start(True);self.assertEqual(self.app.job_id,token)
        release.set();self.pump(lambda:not self.app.busy)
        self.assertIn('已中止',self.app.status.get())
        self.assertIs(self.app.run_result,old)
        self.assertEqual(self.app.tables['comparison'].items,old_items)
        self.assertTrue(Path(old['folder']).is_dir())
        self.app.workspace.extract=original
        self.app.start(True);self.pump(lambda:not self.app.busy)
        self.assertNotEqual(self.app.run_result['id'],old['id'])
        self.assertEqual(str(self.app.stop_btn['state']),'disabled')

    def test_cancel_compare_after_extraction_retains_complete_extract_only(self):
        old=self.complete()
        original,entered,release=self.gate_method('compare')
        self.app.start(True);self.pump(entered.is_set)
        self.assert_stopping();release.set();self.pump(lambda:not self.app.busy)
        self.assertIs(self.app.run_result,old)
        folder=Path(self.app.last_cancelled_output)
        self.assertNotEqual(folder,Path(old['folder']))
        self.assertTrue((folder/'A'/'extraction_report.json').is_file())
        self.assertTrue((folder/'B'/'extraction_report.json').is_file())
        self.assertFalse((folder/'comparison'/'comparison.csv').exists())
        self.assertIn(str(folder),self.app.status.get())
        self.app.workspace.compare=original

    def test_cancel_recompare_preserves_existing_output(self):
        old=self.complete();folder=Path(old['folder'])
        before={p.name:p.read_bytes() for p in (folder/'comparison').iterdir() if p.is_file()}
        original,entered,release=self.gate_method('compare')
        self.app.recompare();self.pump(entered.is_set)
        self.assert_stopping();release.set();self.pump(lambda:not self.app.busy)
        self.assertIs(self.app.run_result,old)
        after={p.name:p.read_bytes() for p in (folder/'comparison').iterdir() if p.is_file()}
        self.assertEqual(before,after)
        self.app.workspace.compare=original
        self.app.recompare();self.pump(lambda:not self.app.busy)
        self.assertIn('比较完成',self.app.status.get())

    def test_cancel_original_search_preserves_diagnostics_then_restarts(self):
        self.complete();before=self.app.diagnostics.get('1.0','end')
        original,entered,release=self.gate_method('search_document')
        self.app.locate_side.set('A');self.app.locate_text.set('AT-X-1')
        self.app.locate();self.pump(entered.is_set)
        self.assert_stopping();release.set();self.pump(lambda:not self.app.busy)
        self.assertEqual(before,self.app.diagnostics.get('1.0','end'))
        self.app.workspace.search_document=original
        self.app.locate();self.pump(lambda:not self.app.busy)
        self.assertIn('第 1 页',self.app.diagnostics.get('1.0','end'))

    def test_stale_progress_and_terminal_do_not_finish_new_job(self):
        entered=threading.Event();release=threading.Event();self.releases.append(release)
        def work(check,emit,context):
            entered.set();release.wait(15);check();return 'located','Current search result'
        self.app.begin_job('新任务',work,'test-stale')
        self.pump(entered.is_set);token=self.app.job_id
        self.app.events.put((token-1,'progress',('stale progress',100)))
        self.app.events.put((token-1,'located','stale text'))
        self.app.poll()
        self.assertTrue(self.app.busy);self.assertEqual(self.app.status.get(),'新任务')
        self.assertNotIn('stale text',self.app.diagnostics.get('1.0','end'))
        self.app.stop_task()
        self.app.events.put((token,'progress',('late progress after stop',90)))
        self.app.poll();self.assertIn('正在中止',self.app.status.get())
        release.set();self.pump(lambda:not self.app.busy)

    def test_late_stop_after_atomic_commit_reports_completed(self):
        old=self.complete()
        original=self.app.workspace.compare
        entered=threading.Event();release=threading.Event();self.releases.append(release)
        def committed(*args,**kwargs):
            result=original(*args,**kwargs)
            entered.set();release.wait(15)
            return result
        self.app.workspace.compare=committed
        self.app.recompare();self.pump(entered.is_set)
        self.assert_stopping();release.set();self.pump(lambda:not self.app.busy)
        self.assertIn('中止请求生效前结果已提交',self.app.status.get())
        self.assertEqual(self.app.run_result['id'],old['id'])
        self.assertEqual(len(self.app.tables['comparison'].items),1)

    def test_stop_button_visible_at_minimum_window(self):
        self.root.geometry('980x660');self.root.update()
        btn=self.app.stop_btn
        self.assertTrue(btn.winfo_ismapped())
        self.assertLessEqual(btn.winfo_rootx()+btn.winfo_width(),self.root.winfo_rootx()+self.root.winfo_width())
        self.assertLessEqual(btn.winfo_rooty()+btn.winfo_height(),self.root.winfo_rooty()+self.root.winfo_height())
        ImageGrab.grab(xdisplay=os.environ['DISPLAY']).save(ROOT/'tests'/'cancel_button_small.png')

    def test_close_requests_stop_and_waits_for_worker(self):
        original,entered,release=self.gate_method('extract')
        self.app.start(True);self.pump(entered.is_set)
        self.app.close()
        self.assertTrue(self.app.closing);self.assertFalse(self.app.closed)
        self.assertTrue(self.app.cancel_event.is_set())
        self.assertTrue(self.root.winfo_exists())
        token=self.app.job_id;self.app.start(True);self.assertEqual(self.app.job_id,token)
        release.set();self.pump(lambda:self.app.closed)


if __name__=='__main__':unittest.main(verbosity=2)
