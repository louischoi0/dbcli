import datetime as dt
import sqlite3
import unittest
from decimal import Decimal

from dbcli.cell_edit import EditError, build_update, literal, quote_ident, source_table
from dbcli.grid import build


class SourceTableTest(unittest.TestCase):
    def test_one_plain_table(self):
        self.assertEqual(source_table("select * from emp where deptno = 10;"), "emp")
        self.assertEqual(source_table("select e.ename from emp e order by 1"), "emp")

    def test_refused(self):
        for sql in ("select * from emp join dept using (deptno)", "select * from emp, dept",
                    "select deptno, count(*) from emp group by deptno", "select distinct job from emp",
                    "with x as (select 1 a from dual) select * from x", "select * from hr.emp",
                    "select * from (select * from emp)", "update emp set sal = 1",
                    "select 1 from a; select 2 from b"):
            with self.assertRaises(EditError, msg=sql):
                source_table(sql)


class BuildUpdateTest(unittest.TestCase):
    def update(self, kind, columns, row, col, text, kinds=None, types=None, table_columns=None):
        g = build(columns, [row], kinds, types)
        _, _, _, kinds, types, numeric = g.source
        return build_update(kind, "emp", columns, row, kinds, types, numeric, col, text, table_columns)

    def test_where_by_every_real_column(self):
        sql, shown = self.update("oracle", ["EMPNO", "ENAME", "COMM", "DBL"], (7369, "SMITH", None, 1600),
                                 1, "O'BRIEN", table_columns={"empno", "ename", "comm"})
        self.assertEqual(sql, "update emp set ENAME = 'O''BRIEN'\n where EMPNO = 7369\n   and ENAME = 'SMITH'\n"
                              "   and COMM IS NULL\n   and rownum = 1;")
        self.assertEqual(shown, "O'BRIEN")

    def test_rowid_finds_the_row(self):
        sql, _ = self.update("oracle", ["ROWID", "SAL"], ("AAAR3sAAEAAAACXAAA", 800), 1, "900")
        self.assertEqual(sql, "update emp set SAL = 900\n where rowid = 'AAAR3sAAEAAAACXAAA';")

    def test_numbers_null_and_alias(self):
        _, shown = self.update("postgres", ["id", "sal"], (1, Decimal("800.5")), 1, "12.25")
        self.assertEqual(shown, Decimal("12.25"))
        sql, shown = self.update("postgres", ["id", "sal"], (1, 2), 1, "null")
        self.assertIn("set sal = NULL", sql)
        self.assertIsNone(shown)
        with self.assertRaises(EditError):
            self.update("postgres", ["id", "sal"], (1, 2), 1, "abc")
        with self.assertRaises(EditError):  # an alias is no column of the table
            self.update("postgres", ["id", "x"], (1, 2), 1, "3", table_columns={"id", "sal"})

    def test_oracle_dates(self):
        sql, _ = self.update("oracle", ["ID", "HIRED"], (1, dt.datetime(2026, 1, 2, 3, 4, 5)), 1,
                             "2026-11-18", types=["N", "D"])
        self.assertIn("set HIRED = TO_DATE('2026-11-18', 'YYYY-MM-DD')", sql)
        self.assertIn("HIRED = TO_DATE('2026-01-02 03:04:05', 'YYYY-MM-DD HH24:MI:SS')", sql)
        with self.assertRaises(EditError):
            self.update("oracle", ["ID", "HIRED"], (1, None), 1, "tomorrow", types=["N", "D"])

    def test_quote_ident(self):
        self.assertEqual(quote_ident("ENAME", "oracle"), "ENAME")
        self.assertEqual(quote_ident("eName", "oracle"), '"eName"')
        self.assertEqual(quote_ident("order", "sqlite"), '"order"')
        self.assertEqual(literal(True, "postgres"), "TRUE")

    def test_runs_on_sqlite(self):
        con = sqlite3.connect(":memory:")
        con.execute("create table emp (id integer, name text, note text)")
        con.executemany("insert into emp values (?, ?, ?)", [(1, "kim", None), (2, "lee", "x")])
        sql, _ = self.update("sqlite", ["id", "name", "note"], (1, "kim", None), 1, "park",
                             table_columns={"id", "name", "note"})
        self.assertEqual(con.execute(sql.rstrip(";")).rowcount, 1)
        self.assertEqual(con.execute("select name from emp order by id").fetchall(), [("park",), ("lee",)])
        con.close()


class GridSetValueTest(unittest.TestCase):
    def test_set_value_under_a_sort(self):
        g = build(["id", "name"], [(2, "b"), (1, "a")])
        g.sort_by(0)
        old = g.set_value(0, 1, "a long name")
        self.assertEqual(old, (1, "a"))
        self.assertEqual(g.value(0, 1), "a long name")
        self.assertEqual(g.original[0], [(2, "b"), (1, "a long name")])
        self.assertEqual(g.widths[1], len("a long name"))


if __name__ == "__main__":
    unittest.main()
