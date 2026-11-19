import datetime as dt
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from dbcli.store import load_last, save_last, session_dir


class StoreTest(unittest.TestCase):
    def test_round_trip_and_owner_only(self):
        root = Path(tempfile.mkdtemp())
        folder = session_dir("prod/ora 1", root)
        self.assertEqual(folder.name, "prod_ora_1")
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        rows = [(1, 2.5, Decimal("3.25"), None, "x", b"\x01\xff", dt.datetime(2026, 11, 18, 5, 0))]
        save_last(folder, {"text": "select 1", "output": [["class:note", "(1 row)"]], "columns": list("abcdefg"),
                           "rows": rows, "title": "t", "trace": ["Execution Plan"]})
        self.assertEqual((folder / "last.json").stat().st_mode & 0o777, 0o600)
        last = load_last(folder)
        self.assertEqual(last["rows"], [(1, 2.5, 3.25, None, "x", b"\x01\xff", "2026-11-18 05:00:00")])
        self.assertEqual((last["text"], last["trace"], last["output"]), ("select 1", ["Execution Plan"],
                                                                          [["class:note", "(1 row)"]]))
        self.assertIn("saved_at", last)

    def test_missing_or_broken_file(self):
        folder = session_dir("x", Path(tempfile.mkdtemp()))
        self.assertIsNone(load_last(folder))
        (folder / "last.json").write_text("{broken")
        self.assertIsNone(load_last(folder))


if __name__ == "__main__":
    unittest.main()


class HistoryTest(unittest.TestCase):
    def test_run_numbers_oldest_first(self):
        from dbcli.store import history_stamp, history_view, load_history
        path = Path(tempfile.mkdtemp()) / "history"
        path.write_text("\n# 2026-11-17 07:32:23.3\n+select 1\n\n# 2026-11-18 05:00:00.1\n+select *\n+from t\n"
                        "\n# 2026-11-18 06:10:00.0\n+select 1\n")
        runs = load_history(path)
        self.assertEqual([t for _, t in runs], ["select 1", "select *\nfrom t", "select 1"])  # run 1 is the oldest
        view = history_view(runs, 10)
        self.assertEqual([(n, t) for n, _, t in view], [(3, "select 1"), (2, "select *\nfrom t")])  # newest first, once
        self.assertEqual(view[0][1], dt.datetime(2026, 11, 18, 6, 10))
        self.assertEqual([n for n, _, _ in history_view(runs, 1)], [3])
        now = dt.datetime(2026, 11, 18, 9, 0)
        self.assertEqual(history_stamp(view[0][1], now), "06:10")
        self.assertEqual(history_stamp(dt.datetime(2026, 11, 17, 7, 32), now), "11-17")
        self.assertEqual(history_stamp(None, now), "     ")
        self.assertEqual(load_history(path.parent / "none"), [])


class SnapshotTest(unittest.TestCase):
    def test_round_trip_once(self):
        import datetime as dt
        import tempfile
        from decimal import Decimal
        from pathlib import Path
        from dbcli.store import save_snapshot, take_snapshot
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            rows = [(1, Decimal("2.5"), b"\x01", dt.datetime(2026, 11, 18, 9, 30), None)]
            save_snapshot(folder, {"cells": [{"note": ["class:note", "hi"]}, {"text": "select 1", "rows": rows}],
                                   "input": {"text": "sel", "cursor": 2}})
            snap = take_snapshot(folder)
            self.assertEqual(snap["input"], {"text": "sel", "cursor": 2})
            self.assertEqual(snap["cells"][0], {"note": ["class:note", "hi"]})
            self.assertEqual(snap["cells"][1]["rows"], [(1, 2.5, b"\x01", "2026-11-18 09:30:00", None)])
            self.assertIsNone(take_snapshot(folder))  # brought back once
            self.assertTrue((folder / "snapshot.prev.json").exists())
