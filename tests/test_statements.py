import unittest

from dbcli.statements import read_only, split, statement_at


class StatementAtTest(unittest.TestCase):
    TEXT = "select 1 from dual;\nselect ';x' from t; -- note\n\nbegin x := case when a then 1 end; if x then null; end if; end;\nselect 3 from"

    def test_the_statement_around_the_cursor(self):
        t = self.TEXT
        self.assertEqual(statement_at(t, 3).text, "select 1 from dual;")
        self.assertEqual(statement_at(t, t.index("';x'")).text, "select ';x' from t; -- note")
        self.assertTrue(statement_at(t, t.index("if x")).text.startswith("begin"))
        self.assertTrue(statement_at(t, t.index("if x")).text.endswith("end if; end;"))
        self.assertIsNone(statement_at(t, len(t)))  # not ended by ;

    def test_a_single_statement_needs_no_semicolon(self):
        self.assertEqual(statement_at("  select 1 from dual ", 4).text, "select 1 from dual")
        self.assertIsNone(statement_at("   ", 1))

    def test_package_spec_is_one_statement(self):
        self.assertEqual(len(split("create or replace package p as procedure a; end p;\nselect 1 from dual;")), 2)

    def test_read_only(self):
        self.assertTrue(read_only("/* x */ with a as (select 1 from dual) select * from a"))
        self.assertTrue(read_only("select 1 from dual"))
        self.assertFalse(read_only("update t set v = 1"))
        self.assertFalse(read_only("begin null; end;"))
        self.assertFalse(read_only("select * from t for update"))
        self.assertFalse(read_only("with a as (select 1 from dual) insert into t select * from a"))
        self.assertFalse(read_only("explain plan for select 1 from dual"))
        self.assertTrue(read_only("select 'update' u from dual -- delete"))


if __name__ == "__main__":
    unittest.main()
