import unittest

from dbcli.db import Result, SqliteDB, changes_schema, format_table, prepare_statement


class PrepareStatementTest(unittest.TestCase):
    def test_whole_input_is_sent(self):
        self.assertEqual(prepare_statement("  select 1\nfrom dual  \n", "oracle"), "select 1\nfrom dual")
        self.assertEqual(prepare_statement("select 1", "sqlite"), "select 1")  # no ';' needed
        self.assertIsNone(prepare_statement(" \n ", "postgres"))

    def test_oracle_trailing_slash_dropped(self):
        self.assertEqual(prepare_statement("begin\n  null;\nend;\n/\n", "oracle"), "begin\n  null;\nend;")
        self.assertEqual(prepare_statement("select 1\n/", "postgres"), "select 1\n/")  # Oracle only


class SqliteTest(unittest.TestCase):
    def test_execute_and_format(self):
        db = SqliteDB(":memory:")
        self.assertEqual(format_table(db.execute("create table t(a int, b text);")), "OK, -1 row(s) affected")
        db.execute("insert into t values (1, 'x'), (2, null);")
        self.assertEqual(format_table(db.execute("select * from t;")),
                         "a | b\n--+-----\n1 | x\n2 | NULL\n(2 rows)")
        self.assertEqual(db.table_columns(), [("t", "a"), ("t", "b")])
        db.execute("create view v as select a from t;")
        self.assertEqual(db.relations(), [("t", "table"), ("v", "view")])
        db.close()

    def test_changes_schema(self):
        self.assertTrue(changes_schema("  CREATE table x(a int);"))
        self.assertTrue(changes_schema("drop view v;"))
        self.assertFalse(changes_schema("select * from created_at;"))

    def test_truncation_note(self):
        res = Result(["n"], [(1,)], 1, truncated=True)
        self.assertTrue(format_table(res).endswith("(1 row, first 1000 shown)"))


if __name__ == "__main__":
    unittest.main()


class ParseErrorTest(unittest.TestCase):
    def test_kinds(self):
        import sqlite3
        from dbcli.db import is_parse_error

        class Pg(Exception):
            def __init__(self, state):
                super().__init__("x")
                self.sqlstate = state

        for msg in ("ORA-00900: invalid SQL statement", "ORA-00942: table or view does not exist",
                    "ORA-00933: SQL command not properly ended", "ORA-06550: line 1, column 7:\nPLS-00201"):
            self.assertTrue(is_parse_error(Exception(msg)), msg)
        for msg in ("ORA-00001: unique constraint violated", "ORA-01400: cannot insert NULL",
                    "DPY-4011: the database or network closed the connection"):
            self.assertFalse(is_parse_error(Exception(msg)), msg)
        self.assertTrue(is_parse_error(Pg("42601")))
        self.assertFalse(is_parse_error(Pg("23505")))
        db = SqliteDB(":memory:")
        for sql in ("selec 1", "select * from nope"):
            with self.assertRaises(sqlite3.Error) as cm:
                db.execute(sql)
            self.assertTrue(is_parse_error(cm.exception), sql)
        db.execute("create table u(a int primary key)"); db.execute("insert into u values (1)")
        with self.assertRaises(sqlite3.Error) as cm:
            db.execute("insert into u values (1)")
        self.assertFalse(is_parse_error(cm.exception))


class PagerTest(unittest.TestCase):
    def test_first_page_then_more(self):
        from dbcli import db as dbmod
        old = dbmod.PAGE_ROWS
        dbmod.PAGE_ROWS = 3
        try:
            d = SqliteDB(":memory:")
            d.execute("create table n(i int)")
            for i in range(7):
                d.execute(f"insert into n values ({i})")
            res = d.execute("select i from n order by i")
            self.assertEqual([r[0] for r in res.rows], [0, 1, 2])
            self.assertIsNotNone(res.more)
            self.assertEqual([r[0] for r in res.more.fetch(3)], [3, 4, 5])
            self.assertFalse(res.more.done)
            self.assertEqual([r[0] for r in res.more.fetch(3)], [6])
            self.assertTrue(res.more.done)
            self.assertIsNone(d.execute("select i from n where i < 2").more)  # it all fit
        finally:
            dbmod.PAGE_ROWS = old


class ErrorTextTest(unittest.TestCase):
    def test_help_link_dropped(self):
        from dbcli.db import error_text
        e = Exception("ORA-00900: invalid SQL statement\nHelp: https://docs.oracle.com/error-help/db/ora-00900/")
        self.assertEqual(error_text(e), "ORA-00900: invalid SQL statement")
        e = Exception("ORA-06550: line 1, column 7:\nPLS-00201: identifier 'X' must be declared\n"
                      "Help: https://docs.oracle.com/error-help/db/ora-06550/")
        self.assertEqual(error_text(e), "ORA-06550: line 1, column 7:\nPLS-00201: identifier 'X' must be declared")
