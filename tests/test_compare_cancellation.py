"""Cooperative stop signals through comparison and report-writing work."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'improved' / 'app'))
import compare_matches as c
from task_control import TaskCancelled


class StopAfter:
    """Simulate a user stop arriving after work has already begun."""
    def __init__(self, checkpoints):
        self.remaining = checkpoints

    def __call__(self):
        self.remaining -= 1
        if self.remaining <= 0:
            raise TaskCancelled()


class CompareCancellation(unittest.TestCase):
    def pair(self, size=20):
        a = {f'AT-X-{i}': [c.Record(f'AT-X-{i}', '安全标题', f'距离为 {i} mm。', i+2)]
             for i in range(size)}
        b = {key: [c.Record(key, '安全标题', value[0].description+'新增内容。', value[0].row)]
             for key, value in a.items()}
        return c.CsvData(Path('A.csv'), 'utf-8', a), c.CsvData(Path('B.csv'), 'utf-8', b)

    def test_already_stopped_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)/'result'
            with self.assertRaises(TaskCancelled):
                c.write_pair(output, *self.pair(), ignore_whitespace=False, cancel_check=StopAfter(1))
            self.assertFalse(output.exists())

    def test_record_comparison_can_stop_partway(self):
        with self.assertRaises(TaskCancelled):
            c.compare(*self.pair(100), cancel_check=StopAfter(20))

    def test_long_common_prefix_observes_stop(self):
        with self.assertRaises(TaskCancelled):
            c.text_diff_segments('甲'*50_000+'乙', '甲'*50_000+'丙', cancel_check=StopAfter(12))

    def test_long_common_suffix_observes_stop(self):
        with self.assertRaises(TaskCancelled):
            c.text_diff_segments('乙'+'甲'*50_000, '丙'+'甲'*50_000, cancel_check=StopAfter(12))

    def test_edit_search_observes_stop(self):
        with self.assertRaises(TaskCancelled):
            c.text_diff('甲乙丙丁'*500, '戊己庚辛'*500, cancel_check=StopAfter(30))

    def test_csv_cancellation_closes_stream(self):
        stop = threading.Event()
        def rows():
            yield {'a': 'first'}
            stop.set()
            yield {'a': 'must not be written'}
        def check():
            if stop.is_set():
                raise TaskCancelled()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'data.csv'
            with self.assertRaises(TaskCancelled):
                c.write_csv(path, ['a'], rows(), cancel_check=check)
            self.assertNotIn('must not be written', path.read_text(encoding='utf-8-sig'))
            path.unlink()  # Also succeeds on Windows only once the stream closed.

    def test_html_rendering_observes_stop(self):
        a, b = self.pair()
        results = c.compare(a, b)
        totals = c.summary(a, b, results, False)
        with self.assertRaises(TaskCancelled):
            c.render_report(a, b, results, totals, cancel_check=StopAfter(25))

    def test_normal_callback_preserves_diff_content(self):
        before = c.text_diff_segments('甲 7 mm\n<乙>', '甲 8 mm\n<乙>')
        self.assertEqual(c.text_diff_segments('甲 7 mm\n<乙>', '甲 8 mm\n<乙>',
                                            cancel_check=lambda: None), before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
