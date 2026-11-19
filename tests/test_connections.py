import tempfile
import unittest
from pathlib import Path

from dbcli import connections as c
from dbcli.config import load

SAMPLE = '''# my config
[ui]
panel = "history"

[templates]
a = "select 1"

# the old sqlite one
[connections.local]
type = "sqlite"
path = "a.db"

[connections.ora]
type = "oracle"
user = "scott"
dsn = "h:1521/ORCL"
password = "tiger"   # keep
# autocommit = false
'''


class ConnectionFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "config.toml"
        self.path.write_text(SAMPLE)

    def tearDown(self):
        self.dir.cleanup()

    def default(self):
        return load(self.path).profiles[0].name

    def test_blocks_round_trip(self):
        self.assertEqual(c.join_blocks(c.split_blocks(SAMPLE)), SAMPLE)
        self.assertEqual(c.names(self.path), ["local", "ora"])

    def test_add_becomes_default_and_keeps_the_rest(self):
        c.add(self.path, "pg", "postgres", {"host": "db", "port": 5432, "dbname": "shop", "user": "app",
                                            "password": 'p"w\\x'})
        cfg = load(self.path)
        self.assertEqual(cfg.profiles[0].name, "pg")
        self.assertEqual(cfg.profiles[0].password(), 'p"w\\x')  # quotes and backslashes survive
        self.assertEqual(cfg.profiles[0].options["port"], 5432)
        text = self.path.read_text()
        self.assertIn("# my config", text)
        self.assertIn('password = "tiger"   # keep', text)
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")

    def test_add_keep_default(self):
        c.add(self.path, "x", "sqlite", {"path": ":memory:"}, make_default=False)
        self.assertEqual(c.names(self.path), ["local", "x", "ora"])
        self.assertEqual(self.default(), "ora")

    def test_add_refuses_duplicates_and_bad_names(self):
        with self.assertRaises(c.ConnectionError_):
            c.add(self.path, "ora", "sqlite", {})
        with self.assertRaises(c.ConnectionError_):
            c.add(self.path, "a b", "sqlite", {})

    def test_set_moves_the_block_with_its_comment(self):
        c.set_default(self.path, "local")
        self.assertEqual(self.default(), "local")
        text = self.path.read_text()
        self.assertTrue(text.rstrip().endswith('# the old sqlite one\n[connections.local]\ntype = "sqlite"\npath = "a.db"'))
        self.assertEqual(load(self.path).templates, {"a": "select 1"})

    def test_comment_under_a_block_stays_with_it(self):
        c.add(self.path, "x", "sqlite", {"path": ":memory:"})
        c.delete(self.path, "x")  # the block after ora: ora keeps its trailing comment
        self.assertIn('password = "tiger"   # keep\n# autocommit = false\n', self.path.read_text())
        c.set_default(self.path, "local")
        self.assertIn('# keep\n# autocommit = false\n', self.path.read_text())

    def test_file_header_comment_stays_put(self):
        new = Path(self.dir.name) / "n.toml"
        c.add(new, "m", "sqlite", {"path": ":memory:"})
        c.delete(new, "m")
        self.assertIn("chmod 600", new.read_text())

    def test_delete(self):
        c.delete(self.path, "ora")
        self.assertEqual(c.names(self.path), ["local"])
        self.assertNotIn("tiger", self.path.read_text())
        with self.assertRaises(c.ConnectionError_):
            c.delete(self.path, "ora")

    def test_add_to_a_new_file(self):
        new = Path(self.dir.name) / "sub" / "config.toml"
        c.add(new, "m", "sqlite", {"path": ":memory:"})
        self.assertEqual([p.name for p in load(new).profiles], ["m"])
        self.assertIn("chmod 600", new.read_text())


if __name__ == "__main__":
    unittest.main()
