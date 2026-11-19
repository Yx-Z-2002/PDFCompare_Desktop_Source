"""Shared exact-text highlighting regressions; no display is required."""
import html
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'improved' / 'app'))
import compare_matches as c


class ColorDiffTests(unittest.TestCase):
    def assert_roundtrip(self, left, right):
        a, b, note = c.text_diff_segments(left, right)
        self.assertEqual(''.join(text for text, tag in a), left)
        self.assertEqual(''.join(text for text, tag in b), right)
        self.assertTrue(all(tag in ('equal', 'delete') for _, tag in a))
        self.assertTrue(all(tag in ('equal', 'insert') for _, tag in b))
        return a, b, note

    def test_chinese_number_replacement(self):
        a, b, note = self.assert_roundtrip('距离为7毫米，范围稳定。', '距离为8毫米，范围稳定。')
        self.assertEqual([text for text, tag in a if tag == 'delete'], ['7'])
        self.assertEqual([text for text, tag in b if tag == 'insert'], ['8'])
        self.assertIn('替换', note)

    def test_unicode_and_html_are_raw(self):
        self.assert_roundtrip('<b>３.２ µm😀</b>\r\n甲\t乙', '<b>３.３ µm🧲</b>\r\n甲  乙')
        a, b, _ = c.text_diff('<script>alert(1)</script>', '<script>alert(2)</script>')
        self.assertNotIn('<script>', a + b)
        self.assertIn('&lt;script&gt;', a + b)

    def test_insert_delete_and_empty(self):
        for a, b in [('', ''), ('', '新增'), ('删除', ''), ('abc', 'a bc'), ('abc def', 'abc')]:
            with self.subTest(a=a,b=b): self.assert_roundtrip(a,b)

    def test_whitespace_diff_keeps_original(self):
        a, b, note = self.assert_roundtrip('甲\t乙\r\n丙', '甲 乙\n丙')
        self.assertIn('\\t', note)
        rendered = c.text_diff('a b', 'a\tb')
        self.assertIn('·', rendered[0]); self.assertIn('⇥', rendered[1])
        self.assertEqual(''.join(t for t, _ in a), '甲\t乙\r\n丙')

    def test_equal_source_has_no_color(self):
        a, b, note = self.assert_roundtrip('相同😀文本\n', '相同😀文本\n')
        self.assertEqual([tag for _, tag in a], ['equal'])
        self.assertEqual(note, '')

    def test_long_text_retains_all_content(self):
        a, b, note = self.assert_roundtrip('共同前缀'+'甲'*3000+'共同尾部', '共同前缀'+'乙'*3000+'共同尾部')
        self.assertIn('整体标记', note)
        self.assertEqual(a[0], ('共同前缀','equal'))
        self.assertEqual(b[-1], ('共同尾部','equal'))

    def test_repeated_chinese_keeps_local_change(self):
        a, b, _ = self.assert_roundtrip('不变'*500+'甲'+'稳定'*500, '不变'*500+'乙'+'稳定'*500)
        self.assertEqual([text for text, tag in a if tag=='delete'], ['甲'])
        self.assertEqual([text for text, tag in b if tag=='insert'], ['乙'])

    def test_html_only_records_are_colored(self):
        a=c.CsvData(Path('a.csv'),'utf-8',{'X':[c.Record('X','A标题','A描述',2)]})
        b=c.CsvData(Path('b.csv'),'utf-8',{'Y':[c.Record('Y','B标题','B描述',2)]})
        items=c.compare(a,b)
        page=c.render_report(a,b,items,c.summary(a,b,items,False))
        self.assertIn('<del>A标题</del>',page)
        self.assertIn('<ins>B描述</ins>',page)


if __name__ == '__main__': unittest.main(verbosity=2)
