import unittest

from dbcli.panes import neighbor

# conns | editor (top) / results (bottom), separated by a 1-col border and title rows
RECTS = {
    "conns": (0, 1, 20, 22),
    "editor": (21, 1, 59, 10),
    "results": (21, 12, 59, 11),
}


class NeighborTest(unittest.TestCase):
    def test_moves_between_panes(self):
        self.assertEqual(neighbor(RECTS, "editor", "j"), "results")
        self.assertEqual(neighbor(RECTS, "results", "k"), "editor")
        self.assertEqual(neighbor(RECTS, "editor", "h"), "conns")
        self.assertEqual(neighbor(RECTS, "results", "h"), "conns")

    def test_h_l_from_tall_pane_follows_cursor_row(self):
        self.assertEqual(neighbor(RECTS, "conns", "l", cursor=(2, 3)), "editor")
        self.assertEqual(neighbor(RECTS, "conns", "l", cursor=(2, 20)), "results")

    def test_no_neighbor_at_edges(self):
        self.assertIsNone(neighbor(RECTS, "editor", "k"))
        self.assertIsNone(neighbor(RECTS, "editor", "l"))
        self.assertIsNone(neighbor(RECTS, "conns", "h"))

    def test_hidden_pane_is_skipped(self):
        rects = {k: v for k, v in RECTS.items() if k != "conns"}
        self.assertIsNone(neighbor(rects, "editor", "h"))


if __name__ == "__main__":
    unittest.main()
