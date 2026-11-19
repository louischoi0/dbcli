import unittest

from dbcli.formatter import NotFormatted, format_sql


class FormatterTest(unittest.TestCase):
    def test_reindents_and_keeps_case(self):
        out = format_sql("select a,b from t x join u y on x.id=y.id where a>1 and b in (select c from d)")
        self.assertEqual(out.splitlines()[:5], ["select a,", "       b", "from t x", "join u y on x.id = y.id",
                                                "where a > 1"])
        self.assertTrue(format_sql("SELECT A FROM T").startswith("SELECT A"))

    def test_hint_and_comment_kept(self):
        out = format_sql("select /*+ full(t) */ v from t -- note")
        self.assertIn("/*+ full(t) */", out)
        self.assertIn("-- note", out)

    def test_left_alone(self):
        for text in ("", "@a 4", "begin null; end;", "declare x number; begin null; end;"):
            with self.assertRaises(NotFormatted):
                format_sql(text)
