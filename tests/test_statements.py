import unittest

from dbcli.db import PLSQL_RE
from dbcli.statements import read_only, script_statements, split, statement_at


class ScriptTest(unittest.TestCase):
    TEXT = """create table a (x number);
-- a note above the next one
insert into a values (1);
-- the block
begin
  dbms_output.put_line('a;b');
end;
/
create index ix on a (x);
-- trailing note"""

    def test_every_statement_in_order(self):
        texts = [s.text for s in script_statements(self.TEXT)]
        self.assertEqual(texts[0], "create table a (x number);")
        self.assertEqual(texts[1], "-- a note above the next one\ninsert into a values (1);")  # its comment along
        self.assertEqual(texts[2], "-- the block\nbegin\n  dbms_output.put_line('a;b');\nend;")  # whole, `;` inside
        self.assertEqual(texts[3], "create index ix on a (x);")  # the `/` line is not part of it
        self.assertEqual(len(texts), 4)  # the trailing comment is no statement

    def test_offsets_point_into_the_cell(self):
        for s in script_statements(self.TEXT):
            self.assertEqual(self.TEXT[s.start:s.end], s.text)

    def test_split_into_cells_loses_nothing_but_blanks(self):
        from dbcli.statements import split_pieces
        self.assertEqual(split_pieces(self.TEXT), [
            "create table a (x number);",
            "-- a note above the next one\ninsert into a values (1);",
            "-- the block\nbegin\n  dbms_output.put_line('a;b');\nend;\n/",  # its `/` along
            "create index ix on a (x);\n-- trailing note",  # the trailing comment with the last
        ])
        self.assertEqual(split_pieces("select 1 from dual"), ["select 1 from dual"])

    def test_a_block_led_by_a_comment_keeps_its_semicolon(self):
        self.assertTrue(PLSQL_RE.match("-- the job\n/* and */ begin null; end;"))
        self.assertFalse(PLSQL_RE.match("-- begin\nselect 1 from dual"))


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
