import unittest

from dbcli.oracle_hints import HINTS
from dbcli.trace import STAT_NAMES, explainable, format_trace, stats_delta
from dbcli.words import WordModel


class TraceTest(unittest.TestCase):
    def test_explainable(self):
        self.assertTrue(explainable("  select * from t"))
        self.assertTrue(explainable("WITH x as (select 1 from dual) select * from x"))
        self.assertTrue(explainable("update t set v = 1"))
        self.assertFalse(explainable("create table x(a int)"))
        self.assertFalse(explainable("begin null; end;"))

    def test_stats_delta_takes_off_the_cost_of_a_reading(self):
        before = {n: 100 for n in STAT_NAMES}
        warm = {n: 102 for n in STAT_NAMES}  # one reading costs 2
        after = {n: 110 for n in STAT_NAMES}  # statement 6 + reading 2
        self.assertEqual(set(stats_delta(before, warm, after).values()), {6})
        self.assertEqual(stats_delta(before, warm, {n: 103 for n in STAT_NAMES})["redo size"], 0)  # never negative

    def test_format_trace(self):
        lines = format_trace(["plan line"], {n: 1 for n in STAT_NAMES}, 3, actual=["actual line"], calls=["call line"])
        titles = [l for i, l in enumerate(lines) if i + 1 < len(lines) and lines[i + 1] == "-" * 58]
        # the estimated plan last, after the actual plan, the call table and the statistics
        self.assertEqual(titles, ["Actual Plan (DISPLAY_CURSOR · ALLSTATS LAST)", "Call Statistics",
                                  "Statistics", "Execution Plan"])
        self.assertEqual(lines[-3:], ["Execution Plan", "-" * 58, "plan line"])
        self.assertIn("          1  consistent gets", lines)
        self.assertIn("          3  rows processed", lines)
        no_stats = format_trace(None, None, 0, "(no plan)", "(no stats)")
        self.assertIn("(no plan)", no_stats)
        self.assertIn("(no stats)", no_stats)


class HintCompletionTest(unittest.TestCase):
    def test_hints_complete_and_follow_case(self):
        w = WordModel()
        w.add_keywords(HINTS, 3)
        self.assertEqual(w.suggest_suffix("select /*+ gath"), "er_plan_statistics")
        self.assertEqual(w.suggest_suffix("select /*+ GATH"), "ER_PLAN_STATISTICS")
        self.assertIn("use_hash", w.candidates("use_"))
        self.assertEqual(w.suggest_suffix("sel"), "ect")  # everyday keywords still win


if __name__ == "__main__":
    unittest.main()


class TraceViewTest(unittest.TestCase):
    PLAN = [
        "Plan hash value: 1",
        " ",
        "---------------------------------------------------",
        "| Id  | Operation                    | Name | Rows  |",
        "---------------------------------------------------",
        "|   0 | SELECT STATEMENT             |      |     2 |",
        "|   1 |  NESTED LOOPS                |      |     2 |",
        "|*  2 |   TABLE ACCESS FULL          | D    |     2 |",
        "|   3 |   TABLE ACCESS BY INDEX ROWID| E    |     1 |",
        "|*  4 |    INDEX RANGE SCAN          | E_IX |     1 |",
        "---------------------------------------------------",
        " ",
        "Predicate Information (identified by operation id):",
        "---------------------------------------------------",
        " ",
        "   2 - filter(\"D\".\"X\"=1)",
        "   4 - access(\"E\".\"D\"=\"D\".\"ID\")",
    ]

    def test_operation_colors(self):
        from dbcli.trace_view import operation_style
        self.assertEqual(operation_style("TABLE ACCESS FULL"), "class:op.full")
        self.assertEqual(operation_style("NESTED LOOPS OUTER"), "class:op.nl")
        self.assertEqual(operation_style("MERGE JOIN CARTESIAN"), "class:op.merge")
        self.assertEqual(operation_style("HASH JOIN RIGHT SEMI"), "class:op.hash")
        self.assertEqual(operation_style("INDEX RANGE SCAN"), "class:op.index")
        self.assertEqual(operation_style("INDEX FAST FULL SCAN"), "class:op.indexfull")
        self.assertEqual(operation_style("TABLE ACCESS BY INDEX ROWID BATCHED"), "class:op.rowid")
        self.assertEqual(operation_style("VIEW"), "class:op")

    def test_tree_and_predicates(self):
        from dbcli.trace import format_trace
        from dbcli.trace_view import render
        lines = ["".join(t for _, t in f) for f in render(format_trace(self.PLAN, None, 2, stats_note="(none)"))]
        tree = [l for l in lines if "LOOPS" in l or "ACCESS" in l or "SCAN" in l]
        self.assertTrue(tree[0].startswith("  1  └─ NESTED LOOPS"))
        self.assertIn("2* ", tree[1]); self.assertIn("├─ TABLE ACCESS FULL", tree[1])
        self.assertIn("└─ TABLE ACCESS BY INDEX ROWID", tree[2])
        self.assertIn("   └─ INDEX RANGE SCAN", tree[3])
        self.assertIn("  4  access(\"E\".\"D\"=\"D\".\"ID\")", lines)
        styles = {st for f in render(format_trace(self.PLAN, None, 2)) for st, _ in f}
        self.assertTrue({"class:op.full", "class:op.nl", "class:op.rowid", "class:op.index"} <= styles)

    def test_far_off_estimate_is_flagged(self):
        from dbcli.trace_view import _far_off
        self.assertTrue(_far_off("200", "4000"))
        self.assertTrue(_far_off("1", "12K"))
        self.assertFalse(_far_off("200", "400"))


class HintHighlightTest(unittest.TestCase):
    def marked(self, text, row=0):
        from prompt_toolkit.document import Document
        from prompt_toolkit.layout.processors import TransformationInput
        from dbcli.ui import SqlHighlight as HintHighlight
        doc = Document(text)
        line = doc.lines[row]
        ti = TransformationInput(None, doc, row, lambda i: i, [("", line)], len(line), 1)
        from prompt_toolkit.layout.utils import explode_text_fragments
        # one fragment per character: the styles are tested, not where the fragments are cut
        frags = explode_text_fragments(HintHighlight().apply_transformation(ti).fragments)
        def show(st, t):
            if "class:hint.arg" in st:
                return "#" if t == "@" else t.upper()  # args drawn as capitals, `@` as #
            if ("class:keyword" in st or "class:symbol" in st) and "class:hint" not in st:
                return "." * len(t)  # keywords are tested on their own below
            if "class:hint.frame" in st:
                return "." * len(t)
            return t if "class:hint" in st else "." * len(t)
        return "".join(show(st, t) for st, t, *_ in frags)

    def test_only_hints_inside_the_comment(self):
        text = "select /*+ full(t) leading */ full from t"
        expect = "." * 11 + "full" + ".T.." + "leading" + "." * (len(text) - 26)
        self.assertEqual(self.marked(text), expect)
        self.assertEqual(self.marked("select /* full */ 1"), "...................")  # not a hint comment

    def test_block_over_lines_and_unclosed(self):
        line = "  USE_HASH(a b) */ *"
        self.assertEqual(self.marked("select /*+\n" + line, row=1), "..USE_HASH.A.B" + "." * (len(line) - 14))
        self.assertEqual(self.marked("select /*+ index_ffs"), "...........index_ffs")

    def test_aliases_in_parentheses(self):
        out = self.marked("select /*+ index(e emp_ix) leading(@sel$1 d) */ x from t")
        self.assertIn("index.E.EMP_IX.", out)  # alias and index name
        self.assertIn("leading.#SEL$1.D.", out)
        self.assertTrue(out.endswith("." * len(" */ x from t")))  # nothing outside the hint


class KeywordHighlightTest(unittest.TestCase):
    def test_keywords_outside_strings_and_comments(self):
        from dbcli.ui import keyword_marks
        text = "SELECT v, 'from x' FROM t -- where\nwhere v.select_list > 1"
        marked = "".join("K" if i in keyword_marks(text) else "." for i in range(len(text)))
        self.assertEqual(marked[:6], "KKKKKK")  # SELECT
        self.assertEqual(marked[text.index("FROM")], "K")
        self.assertNotIn("K", marked[text.index("'from x'"):text.index("'from x'") + 8])  # in a string
        self.assertNotIn("K", marked[text.index("-- where"):text.index("\n")])  # in a comment
        self.assertEqual(marked[text.index("\nwhere") + 1], "K")
        self.assertNotIn("K", marked[text.index("select_list"):])  # part of a name, not a keyword


class SymbolHighlightTest(unittest.TestCase):
    def test_symbols_outside_quotes(self):
        from dbcli.ui import symbol_marks
        text = "select a || ',' , b+1 from t where x <> 'a-b' /* c */"
        marked = "".join("S" if i in symbol_marks(text) else "." for i in range(len(text)))
        for sym, at in (("||", text.index("||")), (",", text.index(" , ") + 1), ("+", text.index("+")),
                        ("<>", text.index("<>")), ("/*", text.index("/*")), ("*/", text.index("*/"))):
            self.assertEqual(marked[at:at + len(sym)], "S" * len(sym), sym)
        self.assertEqual(marked[text.index("','"):text.index("','") + 3], "...")  # inside quotes
        self.assertEqual(marked[text.index("'a-b'"):text.index("'a-b'") + 5], ".....")


class HintOpeningTest(unittest.TestCase):
    def test_where_a_slash_opens_a_hint(self):
        from dbcli.ui import hint_opening
        for before in ("select /", "SELECT /", "update /", "insert  /", "delete /", "merge /",
                       "select * from (select /"):
            self.assertTrue(hint_opening(before, ""), before)
        for before in ("select v /", "select a/", "/", "select 'select /", "-- select /"):
            self.assertFalse(hint_opening(before, ""), before)
        self.assertFalse(hint_opening("select /", "*+ full(t) */"))  # already there


class RanCursorTest(unittest.TestCase):
    """prev_sql_id is checked against the SQL_ID the server hashes from the text."""

    class Cur:
        def __init__(self, server_id, child=2, fail=False):
            self.server_id, self.child, self.fail, self.last = server_id, child, fail, ""

        def setinputsizes(self, **_):
            pass

        def execute(self, sql, **_):
            if self.fail and "dbms_sql_translator" in sql:
                raise RuntimeError("PLS-00201")
            self.last = sql

        def fetchone(self):
            return (self.server_id,) if "dbms_sql_translator" in self.last else (self.child,)

    def test_prev_kept_when_it_is_the_statement(self):
        from dbcli.db import OracleDB
        self.assertEqual(OracleDB._ran_cursor(self.Cur("abc"), "select 1 from dual", ("abc", 1)), ("abc", 1))

    def test_prev_of_another_statement_is_replaced(self):
        from dbcli.db import OWN_STATS_ID, OracleDB
        # a result cut at the row limit: Oracle 23ai named autotrace's own counter query
        self.assertEqual(OracleDB._ran_cursor(self.Cur("abc"), "select 1 from dual", (OWN_STATS_ID, 0)), ("abc", 2))

    def test_without_the_server_hash_own_query_is_dropped(self):
        from dbcli.db import OWN_STATS_ID, OracleDB
        cur = self.Cur("abc", fail=True)
        self.assertIsNone(OracleDB._ran_cursor(cur, "select 1 from dual", (OWN_STATS_ID, 0)))
        self.assertEqual(OracleDB._ran_cursor(cur, "select 1 from dual", ("xyz", 3)), ("xyz", 3))
