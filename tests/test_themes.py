import tempfile
import unittest
from pathlib import Path

from prompt_toolkit.styles import Style

from dbcli.config import ConfigError, load
from dbcli.subscription import save_ui_setting, set_value
from dbcli.themes import DEFAULT_THEME, SKY, THEMES, style_dict


class ThemeTest(unittest.TestCase):
    def test_every_theme_builds_with_the_same_classes(self):
        classes = set(style_dict(SKY))
        for t in THEMES.values():
            self.assertEqual(set(t.colors), set(SKY), t.name)  # no color left out
            self.assertEqual(set(style_dict(t.colors)), classes)
            Style.from_dict(style_dict(t.colors))  # valid colors
        self.assertEqual(DEFAULT_THEME, "sky")
        self.assertGreaterEqual(len(THEMES), 4)

    def test_config_theme(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text("[ai]\nmodel = \"x\"\n")
            self.assertEqual(load(path).ui.theme, "sky")
            save_ui_setting(path, "theme", "dusk")
            self.assertEqual(load(path).ui.theme, "dusk")
            self.assertIn("[ui]\ntheme = \"dusk\"", path.read_text())
            save_ui_setting(path, "theme", "ember")  # replaced in place
            self.assertEqual(path.read_text().count("theme ="), 1)
            path.write_text("[ui]\ntheme = \"neon\"\n")
            with self.assertRaises(ConfigError):
                load(path)

    def test_set_value_in_another_section(self):
        text = "[ui]\npanel = \"history\"\n\n[ai]\nmodel = \"m\"\n"
        self.assertEqual(set_value(text, "theme", "forest", section="ui"),
                         "[ui]\npanel = \"history\"\ntheme = \"forest\"\n\n[ai]\nmodel = \"m\"\n")


if __name__ == "__main__":
    unittest.main()
