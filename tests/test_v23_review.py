"""Independent transaction/recovery checks for cancellation release 2.3."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'improved' / 'app'))
from service import Workspace
from task_control import TaskCancelled
from extract_requirements import extract_pdf, compile_query
from reportlab.pdfgen import canvas


class TransactionReview(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pdfcompare-review-')
        self.base = Path(self.temp.name)
        self.workspace = Workspace(self.base / 'state')
        self.path = self.base / 'completed'
        (self.path / 'comparison').mkdir(parents=True)
        (self.path / 'comparison' / 'report.html').write_bytes(b'previous completed report')
        (self.path / 'settings.json').write_bytes(b'previous settings')
        (self.path / 'all_results.zip').write_bytes(b'previous archive')
        self.workspace.runs['run'] = {
            'id': 'run', 'path': self.path, 'config': {'ignore_whitespace': False},
            'comparison_ready': True, 'summary': {'previous': True},
        }
        self.before = self.snapshot()

    def tearDown(self):
        self.workspace.close()
        self.temp.cleanup()

    def snapshot(self):
        return {p.relative_to(self.path).as_posix(): p.read_bytes()
                for p in self.path.rglob('*') if p.is_file()}

    @staticmethod
    def prepare(run, stage, ignore, update, cancel):
        (stage / 'comparison').mkdir()
        (stage / 'comparison' / 'report.html').write_bytes(b'new report')
        return {'new': True}

    def test_cancel_after_preparing_preserves_previous_complete_outputs(self):
        requested = False
        def update(message, progress=0):
            nonlocal requested
            if progress == 90:
                requested = True
        def checkpoint():
            if requested:
                raise TaskCancelled()
        with mock.patch.object(self.workspace, '_prepare_comparison', self.prepare):
            with self.assertRaises(TaskCancelled):
                self.workspace.compare('run', True, update, cancel_check=checkpoint)
        self.assertEqual(self.snapshot(), self.before)
        self.assertEqual(self.workspace.runs['run']['summary'], {'previous': True})
        self.assertEqual(self.workspace.runs['run']['config'], {'ignore_whitespace': False})
        self.assertFalse(list(self.path.glob('.comparison-*')))

    def test_commit_install_error_rolls_back_every_old_file(self):
        original = Path.replace
        def replace(source, destination):
            destination = Path(destination)
            if (source.name == 'settings.json' and source.parent.name.startswith('.comparison-')
                    and destination == self.path / 'settings.json'):
                raise PermissionError('simulated installed-settings lock')
            return original(source, destination)
        with mock.patch.object(self.workspace, '_prepare_comparison', self.prepare), \
                mock.patch.object(Path, 'replace', replace):
            with self.assertRaises(PermissionError):
                self.workspace.compare('run', True)
        self.assertEqual(self.snapshot(), self.before)
        self.assertEqual(self.workspace.runs['run']['summary'], {'previous': True})

    def test_failed_rollback_preserves_original_report_for_recovery(self):
        original = Path.replace
        def replace(source, destination):
            destination = Path(destination)
            if (source.name == 'settings.json' and source.parent.name.startswith('.comparison-')
                    and destination == self.path / 'settings.json'):
                raise PermissionError('simulated install failure')
            if source.name == 'comparison' and source.parent.name == 'previous':
                raise PermissionError('simulated rollback lock')
            return original(source, destination)
        with mock.patch.object(self.workspace, '_prepare_comparison', self.prepare), \
                mock.patch.object(Path, 'replace', replace):
            with self.assertRaises(Exception):
                self.workspace.compare('run', True)
        recovered = [p for p in self.path.rglob('report.html')
                     if p.read_bytes() == b'previous completed report']
        self.assertTrue(recovered, 'Rollback failure must not delete the backup of the old complete report')
        self.assertEqual(self.workspace.runs['run']['summary'], {'previous': True})


class FieldRecoveryReview(unittest.TestCase):
    def test_wrapped_empty_label_does_not_take_adjacent_fields_value(self):
        with tempfile.TemporaryDirectory(prefix='pdfcompare-field-review-') as temp:
            path = Path(temp) / 'adjacent-field.pdf'
            c = canvas.Canvas(str(path), pagesize=(600, 800))
            c.setFont('Helvetica', 11)
            c.drawString(45, 750, 'AT-X-1: Requirement')
            c.drawString(45, 720, 'Description')
            c.drawString(45, 680, 'Require')
            c.drawString(300, 680, 'Test Level: XGen')
            c.drawString(45, 665, 'Key')
            c.save()
            for no_tables in (False, True):
                with self.subTest(no_tables=no_tables):
                    report = extract_pdf(path, compile_query('XGen'),
                                         field_name='Require Key', no_tables=no_tables)
                    item = report['items'][0]
                    self.assertFalse(item['matched'], 'XGen belongs to Test Level, not Require Key')
                    self.assertNotIn('Test Level', item['field_value'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
