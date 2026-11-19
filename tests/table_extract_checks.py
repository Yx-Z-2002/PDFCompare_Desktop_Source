"""Regression checks for metadata column ownership (no external PDFs needed)."""
import re
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'improved' / 'app'))
import extract_requirements as ex

class FakeRow:
    def __init__(self, number, columns):
        self.bbox = (10, 20 + 20 * number, 10 + 100 * columns, 40 + 20 * number)
        self.cells = [(10 + 100 * i, self.bbox[1], 110 + 100 * i, self.bbox[3]) for i in range(columns)]
class FakeTable:
    def __init__(self, rows):
        self.data = rows
        self.rows = [FakeRow(i, len(row)) for i, row in enumerate(rows)]
        self.bbox = (10, 20, 10 + 100 * len(rows[0]), 20 + 20 * len(rows))
    def extract(self, **kwargs): return self.data
class FakePage:
    bbox = (0, 0, 600, 800)
    height = 800
    def __init__(self, rows): self.table = FakeTable(rows)
    def find_tables(self): return [self.table]

def events(rows):
    fields = {ex.field_key(name): name for name in ('Product Structure', 'Test Level', 'Linked Work Items')}
    id_start = re.compile(r'^\s*(?P<id>' + ex.DEFAULT_ID_PATTERN + r')(?!\w)', re.I)
    return ex.metadata_events(FakePage(rows), fields, id_start=id_start)[0]

class TableChecks(unittest.TestCase):
    def test_separate_pairs(self):
        got = events([['Product Structure', 'Alpha', 'Test Level', 'XGen']])
        self.assertEqual([(e[3][0], e[3][1]) for e in got], [('Product Structure', 'Alpha'), ('Test Level', 'XGen')])
        self.assertEqual(got[0][3][2], (10,20,210,40))
        self.assertEqual(got[1][3][2], (210,20,410,40))
    def test_index_column(self):
        got = events([['1', 'Product Structure', 'XGen']])
        self.assertEqual([(e[3][0],e[3][1]) for e in got], [('Product Structure','XGen')])
    def test_value_named_like_field(self):
        got = events([['Product Structure', 'Test Level']])
        self.assertEqual(got[0][3][1], 'Test Level')
    def test_heading_row_not_swallowed(self):
        got = events([['Product Structure','Alpha'], ['AT-ABC-2', 'New title'], ['Test Level','XGen']])
        self.assertEqual([(e[3][0],e[3][1]) for e in got], [('Product Structure','Alpha'),('Test Level','XGen')])
    def test_link_reference_not_new_requirement(self):
        got = events([['Linked Work Items','AT-ABC-2 - Reference']])
        self.assertEqual(got[0][3][1], 'AT-ABC-2 - Reference')
    def test_unknown_field_stops_continuation(self):
        got = events([['Product Structure','Alpha'],['Other Field','XGen']])
        self.assertEqual(got[1][3][0], 'Other Field')
    def test_blank_label_continues(self):
        got = events([['Product Structure','Alpha'],['','XGen']])
        self.assertEqual(got[1][3][0], '')
        self.assertEqual(got[1][3][1], 'XGen')
    def test_paired_continuations_stay_in_their_columns(self):
        got = events([['Product Structure','Alpha','Test Level','Beta'],['','One','','Two']])
        self.assertEqual([(e[3][0],e[3][1]) for e in got],
            [('Product Structure','Alpha'),('Test Level','Beta'),('Product Structure','One'),('Test Level','Two')])
    def test_unknown_label_in_established_pair(self):
        got = events([['Product Structure','Alpha','Test Level','Beta'],['Product Structure','One','Other Field','Two']])
        self.assertEqual([(e[3][0],e[3][1]) for e in got][-2:], [('Product Structure','One'),('Other Field','Two')])
    def test_four_column_value_named_like_field(self):
        got = events([['Product Structure','Test Level','Test Level','XGen']])
        self.assertEqual([(e[3][0],e[3][1]) for e in got], [('Product Structure','Test Level'),('Test Level','XGen')])
    def test_non_metadata_table(self):
        self.assertEqual(events([['Name','Count'],['Chair','3']]), [])

if __name__ == '__main__': unittest.main(verbosity=2)
