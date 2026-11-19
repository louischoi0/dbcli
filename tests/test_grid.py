import unittest
from decimal import Decimal

from dbcli.grid import build, cell_text, fit, to_text


class GridTest(unittest.TestCase):
    def test_box_table_with_numbers_null_and_korean(self):
        g = build(["id", "name"], [(1, "kim"), (None, "이순신")])
        self.assertEqual(to_text(g), "\n".join([
            "┌──────┬────────┐",
            "│ id   │ name   │",
            "├──────┼────────┤",
            "│    1 │ kim    │",
            "│ NULL │ 이순신 │",
            "└──────┴────────┘",
        ]))
        self.assertEqual(g.rows, 2)
        self.assertEqual(g.col_x, [2, 9])  # where each column's text starts

    def test_styles(self):
        g = build(["n"], [(1,), (None,)])
        self.assertIn("class:grid.header", g.lines[1][1][0])
        self.assertEqual(g.lines[3][1][0], "class:grid.row class:grid.number")
        self.assertEqual(g.lines[4][1][0], "class:grid.row.alt class:grid.null")

    def test_empty_result_keeps_header(self):
        self.assertEqual(to_text(build(["a"], [])), "┌───┐\n│ a │\n├───┤\n└───┘")

    def test_cell_text_and_fit(self):
        self.assertEqual(cell_text(b"\x01\xff"), "0x01ff")
        self.assertEqual(cell_text("a\nb"), "a↵b")
        self.assertEqual(cell_text(Decimal("1.50")), "1.50")
        self.assertEqual(fit("abcdef", 4), "abc…")
        self.assertEqual(fit("가나다라", 5), "가나…")

    def test_long_values_are_cut(self):
        g = build(["v"], [("x" * 100,)])
        self.assertEqual(len(to_text(g).splitlines()[3]), 40 + 4)


if __name__ == "__main__":
    unittest.main()


class ValueStyleTest(unittest.TestCase):
    def test_types(self):
        import datetime as dt
        from decimal import Decimal
        from dbcli.grid import value_style
        cases = [(None, "null"), (True, "bool"), (3, "number"), (2.5, "number"), (Decimal("1.5"), "number"),
                 ("kim", "text"), (dt.datetime(2026, 11, 18, 5), "date"), (dt.date(2026, 11, 18), "date"),
                 ("2026-11-18 05:00:00", "date"), ("2026-11-18", "date"), (b"\x01", "bytes"), ("2026", "text")]
        for v, kind in cases:
            self.assertEqual(value_style(v), f"class:grid.{kind}", repr(v))

    def test_column_type_wins_over_the_value(self):
        from dbcli.db import column_kind
        from dbcli.grid import cell_style

        class T:
            def __init__(self, name):
                self.name = name

        self.assertEqual([column_kind(T(n)) for n in ("DB_TYPE_NUMBER", "DB_TYPE_VARCHAR", "DB_TYPE_TIMESTAMP_TZ",
                                                      "DB_TYPE_RAW", "DB_TYPE_BOOLEAN", "DB_TYPE_CLOB")],
                         ["number", "text", "date", "bytes", "bool", "text"])
        self.assertIsNone(column_kind(25))  # a PostgreSQL OID: the value decides
        self.assertEqual(cell_style("2026-11-18", "text"), "class:grid.text")  # a VARCHAR that looks like a date
        self.assertEqual(cell_style("2026-11-18", None), "class:grid.date")
        self.assertEqual(cell_style(None, "number"), "class:grid.null")
        g = build(["code"], [("007",)], ["text"])
        self.assertIn("class:grid.text", g.lines[3][1][0])

    def test_resize_a_column(self):
        g = build(["id", "name"], [(1, "kim"), (None, "lee")])
        self.assertEqual(g.widths, [4, 4])
        self.assertEqual(g.border_x(0), 7)  # "│ NULL │": the second │
        g.resize(1, 8)
        self.assertEqual(to_text(g).splitlines()[3], "│    1 │ kim      │")
        g.resize(0, 2)  # narrower than the text: cut with …
        self.assertEqual(to_text(g).splitlines()[4], "│ N… │ lee      │")
        g.resize(0, 0)
        self.assertEqual(g.widths[0], 1)  # never below one cell

    def test_sort_cycle_and_nulls_last(self):
        g = build(["n", "s"], [(2, "b"), (None, "c"), (10, "a"), (1, None)])
        col = lambda g, c: [line.split("│")[c].strip() for line in to_text(g).splitlines()[3:-1]]
        g.sort_by(0)
        self.assertEqual(col(g, 1), ["1", "2", "10", "NULL"])  # numbers by value, NULL last
        self.assertIn("▲", to_text(g).splitlines()[1])
        g.sort_by(0)
        self.assertEqual(col(g, 1), ["10", "2", "1", "NULL"])
        self.assertIn("▼", to_text(g).splitlines()[1])
        g.sort_by(0)
        self.assertEqual(col(g, 1), ["2", "NULL", "10", "1"])  # back to the order fetched
        self.assertNotIn("▲", to_text(g)); self.assertNotIn("▼", to_text(g))
        g.sort_by(1)
        self.assertEqual(col(g, 2), ["a", "b", "c", "NULL"])
        self.assertEqual(g.column_at(g.col_x[1]), 1)
        self.assertIsNone(g.column_at(g.border_x(0)))

    def test_sorted_header_keeps_its_width(self):
        g = build(["N", "S"], [(1, "a")], ["number", "text"], ["N", "VCH"])
        before = [len(l) for l in to_text(g).splitlines()]
        g.sort_by(0)
        self.assertEqual([len(l) for l in to_text(g).splitlines()], before)
        self.assertTrue(to_text(g).splitlines()[1].startswith("│ N▲  │"))  # the type gives way to ▲

    def test_value_and_copy_text_follow_the_sort(self):
        from dbcli.grid import copy_text
        g = build(["n", "s"], [(2, "b\nline"), (1, None), (3, b"\x0a\xff")])
        g.sort_by(0)
        self.assertEqual([g.value(r, 0) for r in range(3)], [1, 2, 3])
        self.assertEqual(copy_text(g.value(1, 1)), "b\nline")  # in full, line break kept
        self.assertEqual(copy_text(g.value(0, 1)), "")  # NULL
        self.assertEqual(copy_text(g.value(2, 1)), "0aff")

    def test_extend_keeps_widths_and_sort(self):
        g = build(["n"], [(3,), (1,)])
        g.extend([(2,), (12345,)])
        self.assertEqual((g.rows, g.widths), (4, [5]))  # widened for the longer value
        self.assertIn("│ 12345 │", to_text(g))
        g.resize(0, 2)
        g.extend([(9999999,)])
        self.assertEqual(g.widths, [2])  # a dragged width stays
        g.sort_by(0)  # ascending
        g.extend([(0,)])
        self.assertEqual([g.value(r, 0) for r in range(6)], [0, 1, 2, 3, 12345, 9999999])
        self.assertEqual(g.sort, (0, False))
