import json
import os
import tempfile
import unittest
from pathlib import Path

from dbcli.abbrev import Abbreviations
from dbcli.tab import TabAction, tab_action
from dbcli.templates import TemplateError, Templates
from dbcli.words import WordModel

T = Templates({
    "a": "select * from t where num <= $1",
    "between": "select * from t\nwhere num between $1 and $2",
    "who": "select * from emp where name = $1 and dept = $2",
    "now": "select sysdate from dual",
})


class ExpandTest(unittest.TestCase):
    def test_user_example(self):
        self.assertEqual(T.expand("@a 4"), "select * from t where num <= 4")

    def test_multiple_args_and_multiline_body(self):
        self.assertEqual(T.expand("  @between 1 10;  "), "select * from t\nwhere num between 1 and 10")

    def test_quoted_args_keep_quotes_and_spaces(self):
        self.assertEqual(T.expand("@who 'kim lee' \"Sales\""),
                         "select * from emp where name = 'kim lee' and dept = \"Sales\"")

    def test_zero_arg_template(self):
        self.assertEqual(T.expand("@now"), "select sysdate from dual")

    def test_errors(self):
        with self.assertRaisesRegex(TemplateError, "needs 1 argument"):
            T.expand("@a")
        with self.assertRaisesRegex(TemplateError, "needs 1 argument"):
            T.expand("@a 1 2")
        with self.assertRaisesRegex(TemplateError, "no template"):
            T.expand("@nope 1")
        with self.assertRaisesRegex(TemplateError, "cannot read"):
            T.expand("@a 'open")

    def test_is_call(self):
        self.assertTrue(T.is_call("@a 4"))
        self.assertTrue(T.is_call("@"))
        self.assertFalse(T.is_call("select 1 from dual"))
        self.assertFalse(T.is_call("select @a"))

    def test_listing(self):
        self.assertIn("@a       select * from t where num <= $1", T.listing())
        self.assertIn("@between select * from t where num between $1 and $2", T.listing())
        self.assertIn("no templates", Templates().listing())


class TemplateTabTest(unittest.TestCase):
    def act(self, before, after=""):
        return tab_action(before, after, Abbreviations(), WordModel(), T)

    def test_tab_expands_call_in_place(self):
        self.assertEqual(self.act("@a 4"), TabAction("expand", 4, "select * from t where num <= 4"))

    def test_tab_completes_template_name(self):
        self.assertEqual(self.act("@be"), TabAction("accept", 0, "tween "))
        self.assertEqual(self.act("@a"), TabAction("accept", 0, " "))  # needs an argument next
        self.assertEqual(self.act("@now").kind, "expand")  # no arguments: expand right away
        self.assertEqual(self.act("@").kind, "complete")  # several names: open the menu

    def test_tab_reports_bad_call(self):
        act = self.act("@a")  # complete name -> space; with wrong args -> error
        self.assertEqual(act.kind, "accept")
        self.assertEqual(self.act("@a 1 2").kind, "error")

    def test_non_template_lines_unchanged(self):
        self.assertEqual(self.act("sel"), TabAction("accept", 0, "ect "))
        self.assertEqual(self.act("$a").kind, "expand")


    def test_tab_expands_call_inside_a_line(self):
        self.assertEqual(self.act("select * from (@a 4", ")"),
                         TabAction("expand", 4, "select * from t where num <= 4"))
        self.assertEqual(self.act("select x from dual where 1 = 1\nunion @now"),
                         TabAction("expand", 4, "select sysdate from dual"))
        self.assertEqual(self.act("select * from (@be"), TabAction("accept", 0, "tween "))

    def test_inline_at_needs_a_boundary(self):
        self.assertIsNone(T.call_at("select * from emp@now", ""))  # a database link
        self.assertIsNone(T.call_at("select '@a", "x'"))  # inside a word
        self.assertEqual(T.call_at("x = @a 4", ""), "@a 4")

    def test_inline_bad_call_falls_back_to_usual_tab(self):
        self.assertNotEqual(self.act("select (@a 1 2").kind, "error")


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "templates.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_seeded_from_config_once(self):
        t = Templates({"b": "select 2", "a": "select $1"}, path=self.path)
        self.assertEqual([(x.id, x.alias) for x in t.entries()], [(1, "a"), (2, "b")])
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o600")
        again = Templates({"zz": "select 3"}, path=self.path)  # the file wins from now on
        self.assertEqual(again.names(), ["a", "b"])

    def test_add_update_remove_persist(self):
        t = Templates({}, path=self.path)
        a = t.add("select $1 from dual", alias="x")
        self.assertEqual(t.expand("@x 1"), "select 1 from dual")
        t.update(a.id, alias="y", body="select $1, $2 from dual")
        self.assertEqual(Templates(path=self.path).arity("y"), 2)
        b = t.add("select 1", alias="z")
        t.remove(a.id)
        c = t.add("select 2", alias="w")
        self.assertGreater(c.id, b.id)  # ids are never reused
        self.assertEqual([x.alias for x in Templates(path=self.path).entries()], ["z", "w"])

    def test_alias_checks(self):
        t = Templates({"a": "select 1"}, path=self.path)
        self.assertIn("exists", t.check_alias("a"))
        self.assertIsNone(t.check_alias("a", keep=1))  # renaming to itself
        self.assertIn("letters", t.check_alias("1x"))
        with self.assertRaises(TemplateError):
            t.add("select 2", alias="a")

    def test_other_terminal_changes_are_seen(self):
        mine = Templates({"a": "select 1"}, path=self.path)
        other = Templates(path=self.path)
        other.add("select 2", alias="b")
        st = self.path.stat()
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))  # a later write
        self.assertEqual(mine.names(), ["a", "b"])

    def test_bookmark_without_alias_is_no_template(self):
        t = Templates({}, path=self.path)
        b = t.add("select * from v$session where status = 'ACTIVE'", memo="active sessions")
        self.assertEqual(t.names(), [])  # no @alias: not callable
        self.assertFalse(t)
        self.assertEqual([x.label for x in t.entries()], ["active sessions"])
        t.update(b.id, alias="act")
        self.assertEqual(Templates(path=self.path).expand("@act"), b.body)
        t.update(b.id, alias=None)
        self.assertEqual(Templates(path=self.path).names(), [])
        self.assertIsNone(t.check_alias(None))

    def test_old_templates_file_becomes_bookmarks(self):
        legacy = Path(self.dir.name) / "templates.json"
        legacy.write_text(json.dumps({"next_id": 3, "templates": [{"id": 2, "alias": "a", "body": "select $1"}]}))
        path = Path(self.dir.name) / "bookmarks.json"
        t = Templates({"zz": "select 9"}, path=path, legacy=legacy)
        self.assertEqual([(x.id, x.memo, x.alias) for x in t.entries()], [(2, "", "a")])
        self.assertEqual(json.loads(path.read_text())["bookmarks"][0]["memo"], "")
        plain = t.add("select 1")  # no memo, no alias: a plain bookmark
        self.assertEqual((plain.id, plain.label), (3, "#3"))

    def test_name_of_before_becomes_the_memo(self):
        self.path.write_text(json.dumps({"next_id": 3, "bookmarks": [
            {"id": 1, "name": "a", "alias": "a", "body": "select 1"},  # filled from the alias: no memo
            {"id": 2, "name": "my query", "alias": None, "body": "select 2"}]}))
        self.assertEqual([x.memo for x in Templates(path=self.path).entries()], ["", "my query"])


if __name__ == "__main__":
    unittest.main()
