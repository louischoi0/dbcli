import unittest

from dbcli.ui import Command, common_prefix, palette_completions, palette_match

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


    def test_tab_completes_from_matching_commands(self):
        cmds = [RUN, FMT, Command("Cell", "Format and run", "", (), "cell"),
                Command("Result", "Copy selected row", "Y", ("Y",), "results")]
        self.assertEqual(palette_completions("for", cmds), ("for", ["Format"]))
        self.assertEqual(palette_completions("format s", cmds), ("s", ["SQL"]))  # only Format SQL matches "format"
        self.assertEqual(palette_completions("c", cmds), ("c", ["cell", "Copy"]))  # "Cell" is "cell" again
        self.assertEqual(palette_completions("run ", cmds), ("", []))  # nothing typed yet
        self.assertEqual(palette_completions("format", cmds), ("format", []))  # typed in full

    def test_panel_focus_run_come_first(self):
        cmds = [Command("Cell", "Paste plan", "", (), "cell"), Command("Panel", "Open History tab", "Alt-1", (), "any"),
                Command("Focus", "Prompt (input cell)", "Alt-8", (), "any"),
                Command("Cell", "Format SQL", "", (), "cell"), Command("Run", "Run cell", "Ctrl-O", (), "cell"),
                Command("Result", "Rows", "", (), "results")]
        self.assertEqual(palette_completions("p", cmds)[1][0], "Panel")
        self.assertEqual(palette_completions("f", cmds)[1][0], "Focus")
        self.assertEqual(palette_completions("r", cmds)[1][0], "Run")

    def test_common_prefix(self):
        self.assertEqual(common_prefix(["Selected", "select"]), "Select")
        self.assertEqual(common_prefix(["copy", "cell"]), "c")


if __name__ == "__main__":
    unittest.main()
