import unittest

from dbcli.tkprof import format_call_stats, parse_call_stats

SQL = "select ename from scott.emp where deptno = 10"
# shaped like a real 10046 window: autotrace's own reading (a cached cursor, no text) around
# the statement, recursive SQL written before the dep=0 call it belongs to
TRACE = """\
EXEC #111:c=10,e=20,p=0,cr=0,cu=0,mis=0,r=0,dep=0,og=1,plh=1,tim=1
FETCH #111:c=10,e=20,p=0,cr=0,cu=0,mis=0,r=10,dep=0,og=1,plh=1,tim=2
PARSING IN CURSOR #900 len=37 dep=1 uid=0 oct=3 lid=0 tim=3 hv=1 ad='x' sqlid='a'
select obj# from obj$ where name = :1
END OF STMT
PARSE #900:c=0,e=73,p=0,cr=0,cu=0,mis=0,r=0,dep=1,og=4,plh=2,tim=4
EXEC #900:c=1000,e=500,p=1,cr=0,cu=0,mis=0,r=0,dep=1,og=4,plh=2,tim=5
FETCH #900:c=0,e=50,p=0,cr=3,cu=0,mis=0,r=1,dep=1,og=4,plh=2,tim=6
PARSING IN CURSOR #222 len=45 dep=0 uid=179 oct=3 lid=179 tim=7 hv=2 ad='y' sqlid='b'
select ename from scott.emp
where deptno = 10
END OF STMT
PARSE #222:c=2000,e=3000,p=0,cr=41,cu=0,mis=1,r=0,dep=0,og=1,plh=3,tim=8
EXEC #222:c=0,e=370,p=0,cr=0,cu=0,mis=0,r=0,dep=0,og=1,plh=3,tim=9
FETCH #222:c=10000,e=25000,p=2,cr=7,cu=0,mis=0,r=1,dep=0,og=1,plh=3,tim=10
STAT #222 id=1 cnt=3 pid=0 pos=1 obj=1 op='TABLE ACCESS FULL EMP (cr=8 pr=2)'
FETCH #222:c=0,e=40,p=0,cr=1,cu=0,mis=0,r=2,dep=0,og=1,plh=3,tim=11
EXEC #111:c=10,e=20,p=0,cr=0,cu=0,mis=0,r=0,dep=0,og=1,plh=1,tim=12
FETCH #111:c=10,e=20,p=0,cr=0,cu=0,mis=0,r=10,dep=0,og=1,plh=1,tim=13
PARSING IN CURSOR #222 len=20 dep=0 uid=179 oct=42 lid=179 tim=14 hv=3 ad='z' sqlid='c'
alter session set events '10046 trace name context off'
END OF STMT
PARSE #222:c=0,e=139,p=0,cr=0,cu=0,mis=0,r=0,dep=0,og=0,plh=0,tim=15
EXEC #222:c=0,e=452,p=0,cr=0,cu=0,mis=0,r=0,dep=0,og=0,plh=0,tim=16
""".splitlines()


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.stats = parse_call_stats(TRACE, SQL)

    def test_the_statements_own_calls(self):
        p, e, f = (self.stats.calls[c] for c in ("Parse", "Execute", "Fetch"))
        self.assertEqual((p.count, p.cpu, p.elapsed, p.query), (1, 2000, 3000, 41))
        self.assertEqual((e.count, e.elapsed), (1, 370))
        self.assertEqual((f.count, f.cpu, f.elapsed, f.disk, f.query, f.rows), (2, 10000, 25040, 2, 8, 3))

    def test_autotrace_readings_and_a_reused_cursor_number_are_not_counted(self):
        t = self.stats.total  # cursor #111 (no text) and #222 once it holds the alter session: out
        self.assertEqual((t.count, t.rows, t.query), (4, 3, 49))

    def test_recursive_sql_goes_to_the_call_after_it(self):
        r = self.stats.recursive
        self.assertEqual((r.count, r.cpu, r.elapsed, r.disk, r.query, r.rows), (3, 1000, 623, 1, 3, 1))

    def test_whitespace_case_and_a_trailing_semicolon_do_not_matter(self):
        self.assertIsNotNone(parse_call_stats(TRACE, "SELECT ename FROM scott.emp WHERE deptno = 10;"))

    def test_a_statement_not_in_the_trace(self):
        self.assertIsNone(parse_call_stats(TRACE, "select 1 from dual"))


class FormatTest(unittest.TestCase):
    def test_tkprof_layout(self):
        lines = format_call_stats(parse_call_stats(TRACE, SQL))
        self.assertEqual(lines[0].split(), ["call", "count", "cpu", "elapsed", "disk", "query", "current", "rows"])
        self.assertEqual(lines[2].split(), ["Parse", "1", "0.00", "0.00", "0", "41", "0", "0"])
        self.assertEqual(lines[4].split(), ["Fetch", "2", "0.01", "0.03", "2", "8", "0", "3"])
        self.assertEqual(lines[6].split(), ["total", "4", "0.01", "0.03", "2", "49", "0", "3"])
        self.assertEqual(lines[-1].split()[0:2], ["recursive", "3"])
        self.assertEqual(len({len(l) for l in lines if l}), 1)  # every line the same width: columns line up


if __name__ == "__main__":
    unittest.main()
