import unittest

from dbcli.objects import column_refs, table_refs


def names(sql):
    return [n for _, n in table_refs(sql)]


class TableRefsTest(unittest.TestCase):
    def test_from_join_into_update(self):
        self.assertEqual(names("select * from emp e join dept d on d.id = e.dept"), ["emp", "dept"])
        self.assertEqual(names("select * from emp e, dept d, bonus where 1=1"), ["emp", "dept", "bonus"])
        self.assertEqual(names("insert into log values (1)"), ["log"])
        self.assertEqual(names("update emp set sal = 1"), ["emp"])
        self.assertEqual(names("delete from emp where id = 1"), ["emp"])
        sql = "select x\nfrom   emp"
        self.assertEqual(table_refs(sql), [(sql.index("emp"), "emp")])

    def test_what_it_does_not_judge(self):
        self.assertEqual(names("select * from hr.emp"), [])
        self.assertEqual(names("select * from emp@remote"), [])
        self.assertEqual(names('select * from "MyTab"'), [])
        self.assertEqual(names("select * from table(dbms_xplan.display())"), [])
        self.assertEqual(names("select * from (select * from emp) x"), ["emp"])
        self.assertEqual(names("with recent as (select * from emp) select * from recent"), ["emp"])
        self.assertEqual(names("select extract(year from hired), trim(' ' from name) from emp"), ["emp"])
        self.assertEqual(names("select * from emp for update nowait"), ["emp"])
        self.assertEqual(names("select 'from nowhere' -- from ghost\n from emp /* from x */"), ["emp"])

    def test_alias_keywords_end_the_reference(self):
        self.assertEqual(names("select * from emp where id in (select id from bonus)"), ["emp", "bonus"])
        self.assertEqual(names("select * from emp left outer join dept using (id)"), ["emp", "dept"])


if __name__ == "__main__":
    unittest.main()


class ColumnRefsTest(unittest.TestCase):
    def refs(self, sql):
        found = column_refs(sql)
        if found is None:
            return None
        refs, tables = found
        return [(r.name, r.table) for r in refs], tables

    def test_bare_and_qualified(self):
        self.assertEqual(self.refs("select v, w from t where v > 1"), ([("v", None), ("w", None), ("v", None)], ["t"]))
        self.assertEqual(self.refs("select e.name, d.dname from emp e join dept d on d.id = e.dept_id"),
                         ([("name", "emp"), ("dname", "dept"), ("id", "dept"), ("dept_id", "emp")], ["dept", "emp"]))
        self.assertEqual(self.refs("update emp set sal = sal * 2 where id = :id"),
                         ([("sal", None), ("sal", None), ("id", None)], ["emp"]))

    def test_never_columns(self):
        refs, _ = self.refs("select count(*), nvl(v, 0) total, v as w, sysdate, rownum from t t1 "
                            "where t1.v is not null and level > 0 order by total, w")
        self.assertEqual(refs, [("v", None), ("v", None), ("v", "t")])

    def test_left_alone(self):
        self.assertIsNone(self.refs("create table x (a number)"))
        self.assertIsNone(self.refs("begin null; end;"))
        self.assertIsNone(self.refs("select * from t pivot (sum(v) for k in (1))"))
        self.assertEqual(self.refs("select a from (select 1 a from dual)")[0], [])  # a subquery as a table
        self.assertIsNone(self.refs("select x.a, b from hr.emp x"))  # qualified table: not judged
        self.assertEqual(self.refs("select v from t where v = 'col' -- c\n")[0], [("v", None), ("v", None)])


class TableSlotTest(unittest.TestCase):
    def test_slots(self):
        from dbcli.objects import in_table_slot
        for before in ("select * from ", "select * from em", "select * from emp e, de", "select * from emp, d",
                       "select * from emp e join de", "insert into lo", "update em", "delete from em"):
            self.assertTrue(in_table_slot(before), before)
        for before in ("select em", "select * from emp where de", "select * from emp e join dept d on d.",
                       "select extract(year from hi", "select * from emp for update no", "select 'from x"):
            self.assertFalse(in_table_slot(before), before)


class AliasTest(unittest.TestCase):
    def test_aliases_are_not_columns(self):
        for sql in ("select v as total from t order by total", "select v total from t order by total",
                    "select count(*) cnt from t order by cnt", 'select v as "Total" from t',
                    "select case when v > 1 then 'a' else 'b' end flag from t order by flag",
                    "select sysdate d, rownum rn, null x, level lv from t",
                    "select v, count(*) over (partition by v) cnt from t",
                    "select v + 1 next_v, -v neg, v||'x' txt from t",
                    "select 1 one, 'a' two, date '2020-01-01' three from t order by two"):
            refs, _ = column_refs(sql)
            self.assertEqual({r.name.lower() for r in refs} - {"v"}, set(), sql)
