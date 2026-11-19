import unittest

from dbcli.ui import Command, palette_match

RUN = Command("Run", "Run cell", "Ctrl-O", ("c-o",), "cell")
FMT = Command("Cell", "Format SQL", "Ctrl-U", ("c-u",), "cell")


class PaletteMatchTest(unittest.TestCase):
    def test_every_word_must_appear(self):
        self.assertIsNotNone(palette_match("run cell", RUN))
        self.assertIsNone(palette_match("run sql", RUN))

    def test_searches_group_and_shortcut_too(self):
        self.assertIsNotNone(palette_match("ctrl-u", FMT))
        self.assertIsNotNone(palette_match("cell", FMT))  # its group

    def test_label_start_ranks_first(self):
        self.assertLess(palette_match("format", FMT), palette_match("sql", FMT))
        self.assertEqual(palette_match("", RUN), 0)  # an empty search lists everything


if __name__ == "__main__":
    unittest.main()
