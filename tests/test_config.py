import tempfile
import unittest
from pathlib import Path

from dbcli.config import ConfigError, load


def write(text: str) -> Path:
    f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    f.write(text)
    f.close()
    return Path(f.name)


class ConfigTest(unittest.TestCase):
    def test_defaults_when_missing(self):
        cfg = load(Path("/nonexistent/config.toml"))
        self.assertEqual((cfg.profiles, cfg.ui.panel, cfg.ui.show_panel, cfg.ui.editing_mode),
                         ([], "history", True, "vi"))
        self.assertEqual(cfg.abbreviations["a"], "select * from ")

    def test_ui_panel_and_connections(self):
        cfg = load(write('[ui]\npanel = "navigator"\nshow_panel = false\n'
                         '[connections.x]\ntype = "sqlite"\npath = "a.db"\n'))
        self.assertEqual((cfg.ui.panel, cfg.ui.show_panel), ("schema", False))  # the old name opens the schema browser
        self.assertEqual([(p.name, p.type, p.options) for p in cfg.profiles], [("x", "sqlite", {"path": "a.db"})])

    def test_last_added_connection_is_first(self):
        cfg = load(write('[connections.old]\ntype = "sqlite"\n'
                         '[connections.mid]\ntype = "oracle"\nuser = "u"\npassword = "pw"\n'
                         '[connections.new]\ntype = "sqlite"\n'))
        self.assertEqual([p.name for p in cfg.profiles], ["new", "mid", "old"])
        self.assertEqual(cfg.profiles[1].password(), "pw")

    def test_old_connections_panel_opens_history(self):
        self.assertEqual(load(write('[ui]\npanel = "connections"\n')).ui.panel, "history")

    def test_templates(self):
        cfg = load(write('[templates]\na = "select * from t where num <= $1"\n'
                         'multi = """\nselect *\nfrom t\n"""\n'))
        self.assertEqual(cfg.templates["a"], "select * from t where num <= $1")
        self.assertEqual(cfg.templates["multi"], "select *\nfrom t\n")
        with self.assertRaises(ConfigError):
            load(write('[templates]\na = 3\n'))

    def test_profile_target_hides_password(self):
        from dbcli.config import Profile
        self.assertEqual(Profile("o", "oracle", {"user": "u", "dsn": "h:1521/S", "password": "x"}).target(), "u@h:1521/S")
        self.assertEqual(Profile("p", "postgres", {"dsn": "postgresql://app:secret@db:5432/shop"}).target(),
                         "postgresql://app@db:5432/shop")
        self.assertEqual(Profile("q", "postgres", {"host": "h", "user": "u", "dbname": "d"}).target(), "u@h:5432/d")

    def test_bad_values(self):
        with self.assertRaises(ConfigError):
            load(write('[ui]\npanel = "tables"\n'))
        with self.assertRaises(ConfigError):
            load(write('[ui]\nediting_mode = "nano"\n'))
        with self.assertRaises(ConfigError):
            load(write('[connections.x]\ntype = "mysql"\n'))


if __name__ == "__main__":
    unittest.main()
