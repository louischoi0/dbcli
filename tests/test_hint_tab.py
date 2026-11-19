import unittest

from dbcli.oracle_hints import HINTS
from dbcli.ui import hint_slot
from dbcli.words import WordModel


class HintSlotTest(unittest.TestCase):
    def test_where_a_hint_name_goes(self):
        self.assertEqual(hint_slot("select /*+ "), "x")
        self.assertEqual(hint_slot("SELECT /*+ "), "X")
        self.assertEqual(hint_slot("select /*+ full(e) "), "x")
        self.assertIsNone(hint_slot("select /*+ full("))  # an argument, not a hint
        self.assertIsNone(hint_slot("select /*+ full */ "))  # closed
        self.assertIsNone(hint_slot("select "))


class HintCandidatesTest(unittest.TestCase):
    def test_all_hints_most_used_first(self):
        w = WordModel()
        self.assertEqual(w.hint_candidates(""), list(HINTS))
        w.learn("select /*+ use_hash(a) leading(a b) */ * from t a")
        w.learn("select /*+ leading(x) */ 1 from dual")
        self.assertEqual(w.hint_candidates("")[:2], ["leading", "use_hash"])
        self.assertEqual(len(w.hint_candidates("")), len(HINTS))
        self.assertNotIn("a", w._pairs.get("/*+", {}))  # arguments are not hints
        self.assertTrue(all(h.startswith("no_u") for h in w.hint_candidates("no_u")))


if __name__ == "__main__":
    unittest.main()
