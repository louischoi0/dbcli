import unittest

from dbcli.db import SqliteDB
from dbcli.statements import bind_names



class BindNamesTest(unittest.TestCase):
    def test_names_in_order_each_once_regardless_of_case(self):
        self.assertEqual(bind_names("select * from t where d = :cust_dt and x = :Cust_Dt and y = :1"),
                         ["cust_dt", "1"])

    def test_a_colon_in_strings_quoted_names_and_comments_is_not_a_bind(self):
        sql = """select to_char(sysdate, 'HH24:MI:SS'), q'[a :no]', "a:b", 'it''s :x'
                 from t -- :c
                 where /* :d */ /*+ index(t :e) */ x = :yes"""
        self.assertEqual(bind_names(sql), ["yes"])

    def test_casts_and_plsql_assignments(self):
        self.assertEqual(bind_names("select x::int from t where y = :p"), ["p"])
        self.assertEqual(bind_names("begin :o := 1; end;"), ["o"])

    def test_a_trigger_body_has_no_binds(self):
        self.assertEqual(bind_names("create or replace trigger trg before insert on t for each row "
                                    "begin :new.a := :old.a; end;"), [])

    def test_names_with_digits_and_oracle_name_characters(self):
        self.assertEqual(bind_names("select * from t where a = :x1 and b = :x_2#"), ["x1", "x_2#"])

    def test_none(self):
        self.assertEqual(bind_names("select 1 from dual"), [])


class BindValuesTest(unittest.TestCase):
    def test_sqlite_finds_a_value_for_any_spelling(self):
        db = SqliteDB(":memory:")
        res = db.execute("select :Cust_Dt, :CUST_DT, :n", {"cust_dt": "20240101", "n": None})
        self.assertEqual(res.rows, [("20240101", "20240101", None)])


if __name__ == "__main__":
    unittest.main()
