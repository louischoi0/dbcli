import tempfile
import unittest
from pathlib import Path

from dbcli.abbrev import Abbreviations
from dbcli.tab import TabAction, tab_action
from dbcli.words import WordModel


class AbbrevTest(unittest.TestCase):
    def setUp(self):
        self.ab = Abbreviations({"a": "select * from ", "cnt": "select count(*) from "})

    def test_expands_token_at_cursor(self):
        e = self.ab.match("$a")
        self.assertEqual((e.token, e.body), ("$a", "select * from "))
        self.assertEqual(self.ab.match("foo; $cnt").token, "$cnt")

    def test_unknown_or_embedded_tokens_do_not_expand(self):
        self.assertIsNone(self.ab.match("$zz"))
        self.assertIsNone(self.ab.match("select * from v$a"))  # Oracle v$ views
        self.assertIsNone(self.ab.match("$a "))


class WordModelTest(unittest.TestCase):
    def test_se_and_sel_suggest_select(self):
        w = WordModel()
        self.assertEqual(w.suggest_suffix("se"), "lect")
        self.assertEqual(w.suggest_suffix("sel"), "ect")
        self.assertEqual(w.suggest_suffix("SEL"), "ECT")
        self.assertEqual(w.suggest_suffix("Sel"), "ect")

    def test_no_suggestion_for_short_complete_or_mid_word(self):
        w = WordModel()
        self.assertEqual(w.suggest_suffix("s"), "")
        self.assertEqual(w.suggest_suffix("select"), "")
        self.assertEqual(w.suggest_suffix("se", after="lect"), "")
        self.assertEqual(w.suggest_suffix("se", after=" x"), "lect")

    def test_usage_outweighs_keyword_base(self):
        w = WordModel()
        for _ in range(60):
            w.learn("delete from SESSIONS_LOG")
        self.assertEqual(w.candidates("se", 2), ["sessions_log", "select"])
        self.assertEqual(w.suggest_suffix("SE"), "SSIONS_LOG")

    def test_complete_word_beats_longer_rarer_word(self):
        w = WordModel()
        w.learn("select selectivity")
        self.assertEqual(w.suggest_suffix("select"), "")

    def test_mixed_case_names_keep_their_spelling(self):
        w = WordModel()
        w.add_base("MyTable", 5)
        self.assertEqual(w.suggest("from my"), ("my", "MyTable"))
        self.assertEqual(w.suggest("MYT"), ("MYT", "MyTable"))
        self.assertEqual(w.suggest_suffix("my"), "Table")  # gray text after the cursor
        self.assertEqual(w.candidates("my"), ["MyTable"])  # menu replaces the prefix
        w.add_base("ORDERS", 5)  # single-case names still follow how you type
        self.assertEqual(w.candidates("ord", 1), ["order"])
        self.assertIn("orders", w.candidates("ord"))

    def test_identifiers_keep_their_form_and_oracle_chars(self):
        w = WordModel()
        w.learn("select * from v$session where status = 'customer_x' -- custom")
        self.assertEqual(w.suggest_suffix("from v$s"), "ession")
        self.assertEqual(w.candidates("custo"), [])  # literals and comments ignored

    def test_persistence_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "words.db"
            w = WordModel(p)
            w.learn("select order_items from orders")
            w.save()
            w2 = WordModel(p)
            self.assertEqual(w2.suggest_suffix("order_"), "items")
            w2.save()  # saving again must not duplicate or fail
            self.assertEqual(WordModel(p).score("orders"), 1)


class TabTest(unittest.TestCase):
    def setUp(self):
        self.ab = Abbreviations({"a": "select * from "})
        self.w = WordModel()

    def test_abbreviation_wins(self):
        self.assertEqual(tab_action("$a", "", self.ab, self.w), TabAction("expand", 2, "select * from "))

    def test_accepts_suggestion_with_trailing_space(self):
        self.assertEqual(tab_action("sel", "", self.ab, self.w), TabAction("accept", 0, "ect "))
        self.assertEqual(tab_action("sel", " *", self.ab, self.w), TabAction("accept", 0, "ect"))

    def test_accept_rewrites_prefix_for_mixed_case(self):
        self.w.add_base("MyTable", 5)
        self.assertEqual(tab_action("from my", "", self.ab, self.w), TabAction("accept", 2, "MyTable "))
        self.w.add_base("orders", 5)
        self.assertEqual(tab_action("from ORDERS_", "", self.ab, self.w).kind, "complete")

    def test_falls_back_to_menu(self):
        self.assertEqual(tab_action("select ", "", self.ab, self.w).kind, "complete")


if __name__ == "__main__":
    unittest.main()


class ContextCompletionTest(unittest.TestCase):
    def test_statement_columns_first_and_only_after_alias(self):
        w = WordModel()
        for word in ("status", "state_code", "statement_id"):
            w.add_base(word, 5)
        w.context = lambda: (None, None)
        self.assertEqual(w.candidates("stat", 1), ["status"])  # shortest first without context
        w.context = lambda: ({"statement_id"}, None)
        self.assertEqual(w.candidates("stat", 1), ["statement_id"])  # a column of the statement's table
        self.assertEqual(w.suggest_suffix("select stat"), "ement_id")
        w.context = lambda: ({"statement_id", "status"}, {"status"})
        self.assertEqual(w.candidates("st"), ["status"])  # after `alias.`: that table's columns only

    def test_table_slot_offers_extra_names_first(self):
        w = WordModel()
        w.add_base("emp_id", 5)  # a column, from the schema words
        w.context = lambda: ((lambda k: 60.0 if k == "emp" else 30.0 if k == "emp_hist" else 0.0), None,
                             ["emp", "emp_hist", "employees_v"])
        self.assertEqual(w.candidates("em", 3), ["emp", "emp_hist", "emp_id"])
        self.assertEqual(w.suggest_suffix("select * from em"), "p")
        self.assertEqual(w.candidates("EM", 1), ["EMP"])  # follows the typed case


class StatisticsTest(unittest.TestCase):
    def test_sequence_ranks_what_followed_before(self):
        w = WordModel()
        for word in ("emp", "employees"):
            w.add_base(word, 5)
        for _ in range(3):
            w.learn("select * from employees e where e.id = 1")
        w.learn("select emp_x from dual")
        # after FROM, the table that followed FROM before wins over a shorter one
        self.assertEqual(w.candidates("em", 1, before="select * from em"), ["employees"])

    def test_recency_fades(self):
        w = WordModel()
        w.add_base("alpha_old", 5); w.add_base("alpha_new", 5)
        day = 86400
        w.learn("select alpha_old", now=0)
        w.learn("select alpha_new", now=10 * day)
        w.refresh(now=10 * day)
        self.assertEqual(w.candidates("alpha", 1), ["alpha_new"])

    def test_terminals_add_up_in_the_shared_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "words.db"
            a, b = WordModel(p), WordModel(p)
            a.learn("select orders"); a.save()
            b.learn("select orders"); b.save()
            b.refresh()
            self.assertEqual(b.score("orders"), 2)  # both terminals' uses, none written over
            self.assertEqual(WordModel(p).score("orders"), 2)
            self.assertGreater(WordModel(p).sequence("select", "orders"), 0)

    def test_functions_are_candidates(self):
        w = WordModel()
        self.assertIn("to_char", w.candidates("to_"))
        self.assertIn("nvl", w.candidates("nv"))
