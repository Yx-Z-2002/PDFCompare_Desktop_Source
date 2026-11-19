"""Cancellation must stop work, clean incomplete output and preserve old results."""
from __future__ import annotations
import copy
import io
import json
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'improved'/'app'))
sys.path.insert(0,str(ROOT/'tests'))
import service
from regression_service import make_pdf,block
from task_control import TaskCancelled


class CancellationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pdfcompare-cancel-')
        self.root=Path(self.temp.name)
        self.ws=service.Workspace(self.root/'state')
        self.stop=threading.Event()
        self.a=make_pdf(self.root/'A.pdf',[block(),block('AT-X-2',y=720)])
        self.b=make_pdf(self.root/'B.pdf',[block(desc='The limit is 6 mm.'),block('AT-X-2',y=720)])
        self.ids=[self.register(p) for p in (self.a,self.b)]
        self.config={'field':'Require Key','query':'XGen','output_root':str(self.root/'out'),'task_name':'test'}

    def tearDown(self):
        self.ws.close();self.temp.cleanup()

    def check(self):
        if self.stop.is_set():raise TaskCancelled()

    def register(self,path):
        with path.open('rb') as stream:
            return self.ws.register_document(stream,path.stat().st_size,path.name)['id']

    def extract(self,update=lambda *args:None):
        return self.ws.extract(*self.ids,self.config,update,cancel_check=self.check)

    def complete(self):
        run=self.extract();self.ws.compare(run['id'],False)
        return run

    def snapshot(self,path):
        return {p.relative_to(path).as_posix():p.read_bytes() for p in path.rglob('*') if p.is_file()}

    def test_cancel_before_registration_creates_nothing(self):
        self.stop.set();before=set(self.ws.temp.iterdir())
        with self.assertRaises(TaskCancelled):
            self.ws.register_document(io.BytesIO(self.a.read_bytes()),self.a.stat().st_size,'A.pdf',cancel_check=self.check)
        self.assertEqual(set(self.ws.temp.iterdir()),before)
        self.assertEqual(len(self.ws.documents),2)

    def test_cancel_during_stream_copy_removes_partial_input(self):
        parent=self
        class CancellingStream(io.BytesIO):
            def read(self,*args):
                data=super().read(*args);parent.stop.set();return data
        before=set(self.ws.temp.iterdir())
        with self.assertRaises(TaskCancelled):
            self.ws.register_document(CancellingStream(self.a.read_bytes()),self.a.stat().st_size,'A.pdf',cancel_check=self.check)
        self.assertEqual(set(self.ws.temp.iterdir()),before)
        self.assertEqual(len(self.ws.documents),2)

    def test_cancel_during_a_page_removes_only_new_run(self):
        previous=self.complete();before=self.snapshot(Path(previous['folder']))
        def update(message,*args):
            if '正在提取 PDF A' in message:self.stop.set()
        with self.assertRaises(TaskCancelled):self.extract(update)
        self.assertEqual(list((self.root/'out').iterdir()),[Path(previous['folder'])])
        self.assertEqual(self.snapshot(Path(previous['folder'])),before)
        self.assertEqual(len(self.ws.runs),1)

    def test_cancel_when_b_starts_removes_completed_a_from_new_run(self):
        def update(message,*args):
            if message.startswith('分析 PDF B'):self.stop.set()
        with self.assertRaises(TaskCancelled):self.extract(update)
        self.assertFalse(list((self.root/'out').iterdir()))
        self.assertFalse(self.ws.runs)

    def test_cancel_during_csv_export_removes_new_run(self):
        original=service.write_csv
        def writing(*args,**kwargs):
            original(*args,**kwargs);self.stop.set()
        with patch.object(service,'write_csv',side_effect=writing):
            with self.assertRaises(TaskCancelled):self.extract()
        self.assertFalse(list((self.root/'out').iterdir()))
        self.assertFalse(self.ws.runs)

    def test_cancel_at_extraction_completion_does_not_publish_partial_run(self):
        def update(message,progress=0):
            if progress==100:self.stop.set()
        with self.assertRaises(TaskCancelled):self.extract(update)
        self.assertFalse(self.ws.runs)
        self.assertFalse(list((self.root/'out').iterdir()))
        self.stop.clear()
        run=self.extract()
        self.assertTrue(Path(run['folder'],'A','matches.csv').exists())
        self.assertEqual(len(self.ws.runs),1)

    def test_recompare_cancel_before_audit_preserves_every_previous_byte(self):
        run=self.complete();path=Path(run['folder']);before=self.snapshot(path)
        state=copy.deepcopy(self.ws.run(run['id']))
        def update(message,*args):
            if '核对未命中' in message:self.stop.set()
        with self.assertRaises(TaskCancelled):self.ws.compare(run['id'],True,update,cancel_check=self.check)
        self.assertEqual(self.snapshot(path),before)
        self.assertEqual(self.ws.run(run['id']),state)
        self.assertFalse(list(path.glob('.comparison-*')))

    def test_recompare_cancel_after_new_csv_preserves_previous_comparison(self):
        run=self.complete();path=Path(run['folder']);before=self.snapshot(path)
        original=service.comparator.write_pair
        def writing(*args,**kwargs):
            result=original(*args,**kwargs);self.stop.set();return result
        with patch.object(service.comparator,'write_pair',side_effect=writing):
            with self.assertRaises(TaskCancelled):self.ws.compare(run['id'],True,cancel_check=self.check)
        self.assertEqual(self.snapshot(path),before)
        self.assertFalse(self.ws.run(run['id'])['config']['ignore_whitespace'])

    def test_cancel_while_archiving_preserves_old_zip_and_reports(self):
        run=self.complete();path=Path(run['folder']);before=self.snapshot(path)
        original=zipfile.ZipFile.open
        def opening(archive,name,mode='r',*args,**kwargs):
            stream=original(archive,name,mode,*args,**kwargs)
            if mode=='w' and name=='comparison/comparison.csv':self.stop.set()
            return stream
        with patch.object(zipfile.ZipFile,'open',new=opening):
            with self.assertRaises(TaskCancelled):self.ws.compare(run['id'],True,cancel_check=self.check)
        self.assertEqual(self.snapshot(path),before)
        self.stop.clear()
        self.ws.compare(run['id'],True,cancel_check=self.check)
        with zipfile.ZipFile(path/'all_results.zip') as archive:
            self.assertIsNone(archive.testzip())
            self.assertFalse(any(name.startswith('.comparison-') for name in archive.namelist()))
            self.assertTrue(json.loads(archive.read('settings.json'))['ignore_whitespace'])

    def test_cancel_first_comparison_retains_completed_extraction(self):
        run=self.extract();path=Path(run['folder']);before=self.snapshot(path)
        self.stop.set()
        with self.assertRaises(TaskCancelled):self.ws.compare(run['id'],False,cancel_check=self.check)
        self.assertEqual(self.snapshot(path),before)
        self.assertFalse(self.ws.run(run['id'])['comparison_ready'])
        self.assertFalse((path/'comparison').exists())

    def test_cancel_search_and_search_again(self):
        def progress(*args):self.stop.set()
        with self.assertRaises(TaskCancelled):self.ws.search_document(self.ids[0],'AT-X',progress,cancel_check=self.check)
        self.stop.clear()
        result=self.ws.search_document(self.ids[0],'AT-X',cancel_check=self.check)
        self.assertEqual(len(result['hits']),2)

    def test_cancel_from_independent_audit_is_not_hidden(self):
        a=service.comparator.CsvData(Path('a'),'utf-8',{'AT-X-99':[service.comparator.Record('AT-X-99','Title','Body',2)]})
        b=service.comparator.CsvData(Path('b'),'utf-8',{})
        with patch.object(service,'searchable_pages',side_effect=TaskCancelled):
            with self.assertRaises(TaskCancelled):
                service.comparison_audit([{'items':[]},{'items':[]}],a,b,[self.ws.document(x) for x in self.ids])

    def test_commit_failure_restores_previous_files(self):
        run=self.complete();path=Path(run['folder']);before=self.snapshot(path)
        original=Path.replace
        def replacing(source,target):
            if source.name=='settings.json' and source.parent.name.startswith('.comparison-'):
                raise PermissionError('simulated locked settings')
            return original(source,target)
        with patch.object(Path,'replace',new=replacing):
            with self.assertRaises(PermissionError):self.ws.compare(run['id'],True)
        self.assertEqual(self.snapshot(path),before)
        self.assertFalse(self.ws.run(run['id'])['config']['ignore_whitespace'])


if __name__=='__main__':unittest.main(verbosity=2)
