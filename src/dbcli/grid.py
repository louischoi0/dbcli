"""Box-drawn result table as styled fragment lines (no UI state, so it is testable).

┌──────┬────────┐
│ id   │ name   │   numbers right-aligned, values colored by type, NULL dimmed, wide (CJK) text measured
├──────┼────────┤
│    1 │ kim    │
│ NULL │ 東京   │
└──────┴────────┘
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal

from prompt_toolkit.utils import get_cwidth

MAX_COL_WIDTH = 40  # a column's width as built; dragging its border may make it wider
MAX_DRAG_WIDTH = 400
HEADER_LINES = 3  # top rule, column names, separator
BORDER = "class:grid.border"

Fragments = list[tuple[str, str]]


@dataclass
class Grid:
    lines: list[Fragments]
    col_x: list[int]  # display column where each cell's text starts
    rows: int
    widths: list[int] = field(default_factory=list)  # each column's text width
    source: tuple | None = field(default=None, repr=False)  # what it was built from, to redraw
    sort: tuple[int, bool] | None = None  # (column, descending) the rows are sorted by
    dragged: set[int] = field(default_factory=set)  # columns whose width the user set
    original: tuple | None = field(default=None, repr=False)  # rows and cells in the order fetched

    def border_x(self, i: int) -> int:
        """Display column of the `│` right of column i."""
        return self.col_x[i] + self.widths[i] + 1

    def resize(self, i: int, width: int) -> None:
        """Set column i's width (a dragged border) and redraw the table."""
        if self.source is None or not 0 <= i < len(self.widths):
            return
        self.widths[i] = max(1, min(MAX_DRAG_WIDTH, width))
        self.dragged.add(i)
        self._redraw()

    def value(self, row: int, col: int):
        """The value at a row (in the order shown) and column, as fetched."""
        return self.source[1][row][col]

    def column_at(self, x: int) -> int | None:
        """The column whose cell spans display column x (its borders excluded)."""
        for i in range(len(self.widths)):
            if self.col_x[i] - 1 <= x < self.border_x(i):
                return i
        return None

    def sort_by(self, i: int) -> None:
        """Ascending, then descending, then the order fetched: each call takes the next step.
        NULLs go last either way."""
        if self.source is None or not 0 <= i < len(self.widths):
            return
        columns, rows, cells, kinds, types, numeric = self.source
        if self.original is None:
            self.original = (rows, cells)
        if self.sort is None or self.sort[0] != i:
            self.sort = (i, False)
        elif not self.sort[1]:
            self.sort = (i, True)
        else:
            self.sort = None
        rows, cells = self.original
        if self.sort is not None:
            col, desc = self.sort
            order = list(range(len(rows)))
            present = [n for n in order if rows[n][col] is not None]
            nulls = [n for n in order if rows[n][col] is None]
            present.sort(key=lambda n: sort_key(rows[n][col]), reverse=desc)
            order = present + nulls
            rows, cells = [rows[n] for n in order], [cells[n] for n in order]
        self.source = (columns, rows, cells, kinds, types, numeric)
        self._redraw()

    def extend(self, new_rows: list[tuple]) -> None:
        """Rows fetched later (scrolling down a large result) added at the end; a column widens
        for longer values (up to the usual cap) unless its width was dragged; under a sort they
        take their places in it."""
        if self.source is None or not new_rows:
            return
        columns, rows, cells, kinds, types, numeric = self.source
        new_cells = [[cell_text(v) for v in r] for r in new_rows]
        for i in range(len(self.widths)):
            if i not in self.dragged:
                longest = max(get_cwidth(c[i]) for c in new_cells)
                self.widths[i] = max(self.widths[i], min(MAX_COL_WIDTH, longest))
        if self.original is not None:
            orig_rows, orig_cells = self.original
            self.original = (orig_rows + list(new_rows), orig_cells + new_cells)
            state, self.sort = self.sort, None
            self.source = (columns, *self.original, kinds, types, numeric)
            if state is not None:  # sort_by steps on: from the step before, it lands on `state`
                self.sort = (state[0], False) if state[1] else None
                self.sort_by(state[0])
        else:
            self.source = (columns, rows + list(new_rows), cells + new_cells, kinds, types, numeric)
        self.rows = len(self.source[1])
        self._redraw()

    def _redraw(self) -> None:
        self.lines, self.col_x = _render(*self.source, self.widths, self.sort)


def sort_key(v):
    """Numbers, then dates, then text, then bytes; within a kind, by value."""
    if is_number(v):
        return (0, float(v), "")
    if isinstance(v, (dt.datetime, dt.date)):
        return (1, 0.0, v.isoformat())
    if isinstance(v, (bytes, bytearray)):
        return (3, 0.0, bytes(v).hex())
    return (2, 0.0, str(v))


def cell_text(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (bytes, bytearray)):
        return "0x" + bytes(v[:32]).hex() + ("…" if len(v) > 32 else "")
    return str(v).replace("\r\n", "↵").replace("\n", "↵").replace("\t", " ")


def copy_text(v) -> str:
    """A value as copied: in full, line breaks kept; NULL as nothing, bytes as hex."""
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).hex()
    return str(v)


def is_number(v) -> bool:
    return isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)


# a date or time as text (a restored result keeps them as strings): 2026-11-18, 2026-11-18 05:00:00, 05:00:00
DATE_TEXT_RE = re.compile(r"\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?|\d{2}:\d{2}:\d{2}(\.\d+)?")


def value_style(v) -> str:
    """The style of a value by its type: numbers, text, dates and times, booleans, bytes, NULL."""
    if v is None:
        return "class:grid.null"
    if isinstance(v, bool):
        return "class:grid.bool"
    if is_number(v):
        return "class:grid.number"
    if isinstance(v, (dt.date, dt.time, dt.timedelta)):
        return "class:grid.date"
    if isinstance(v, (bytes, bytearray)):
        return "class:grid.bytes"
    if isinstance(v, str) and DATE_TEXT_RE.fullmatch(v):
        return "class:grid.date"
    return "class:grid.text"


def fit(text: str, width: int) -> str:
    """Cut to `width` display cells, ending in … when something was dropped."""
    if get_cwidth(text) <= width:
        return text
    out, used = [], 0
    for ch in text:
        w = get_cwidth(ch)
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def pad(text: str, width: int, right: bool = False) -> str:
    gap = " " * max(0, width - get_cwidth(text))
    return gap + text if right else text + gap


def cell_style(v, kind: str | None) -> str:
    """A cell's text color: the column's type when the driver told it, else the value's."""
    if v is None:
        return "class:grid.null"
    return f"class:grid.{kind}" if kind else value_style(v)


def build(columns: list[str], rows: list[tuple], kinds: list[str | None] | None = None,
          types: list[str] | None = None) -> Grid:
    """The table; `kinds` color the cells by column type, `types` (N, VCH …) label the header."""
    cells = [[cell_text(v) for v in r] for r in rows]
    kinds = list(kinds) if kinds and len(kinds) == len(columns) else [None] * len(columns)
    types = list(types) if types and len(types) == len(columns) else [""] * len(columns)
    # the header holds the name on the left and the type on the right, one space apart
    heads = [get_cwidth(c) + (1 + len(t) if t else 0) for c, t in zip(columns, types)]
    widths = [min(MAX_COL_WIDTH, max([heads[i]] + [get_cwidth(r[i]) for r in cells]))
              for i in range(len(columns))]
    numeric = [kinds[i] == "number" or (bool(rows) and any(is_number(r[i]) for r in rows)
               and all(r[i] is None or is_number(r[i]) for r in rows)) for i in range(len(columns))]
    source = (columns, rows, cells, kinds, types, numeric)
    lines, col_x = _render(*source, widths)
    return Grid(lines, col_x, len(rows), widths, source)


def _render(columns, rows, cells, kinds, types, numeric, widths, sort=None) -> tuple[list[Fragments], list[int]]:
    """The table's lines at the given column widths, and where each column's text starts."""

    def rule(left: str, mid: str, right: str) -> Fragments:
        return [(BORDER, left + mid.join("─" * (w + 2) for w in widths) + right)]

    def line(texts: list[str], styles: list[str]) -> Fragments:
        out: Fragments = [(BORDER, "│")]
        for i, (text, style) in enumerate(zip(texts, styles)):
            out.append((style, " " + pad(fit(text, widths[i]), widths[i], right=numeric[i]) + " "))
            out.append((BORDER, "│"))
        return out

    def header() -> Fragments:
        out: Fragments = [(BORDER, "│")]
        for i, name in enumerate(columns):
            sorted_here = sort is not None and sort[0] == i
            need = 2 if sorted_here else 1  # the sorted column's name takes ▲ / ▼ after it
            # a column too narrow for its name and the type drops the type
            label = types[i] if widths[i] >= len(types[i]) + 1 + need else ""
            room = widths[i] - (1 + len(label) if label else 0)
            if sorted_here:  # ▲ ascending, ▼ descending
                arrow = "▼" if sort[1] else "▲"
                text = fit(name, room - 1) + arrow if room >= 2 else arrow
            else:
                text = fit(name, max(1, room))
            out.append(("class:grid.header", " " + pad(text, room)))
            if label:
                out.append((f"class:grid.type.{kinds[i]}" if kinds[i] else "class:grid.type",
                            " " + label))
            out.append(("class:grid.header", " "))
            out.append((BORDER, "│"))
        return out

    lines = [rule("┌", "┬", "┐"), header(), rule("├", "┼", "┤")]
    for n, (raw, texts) in enumerate(zip(rows, cells)):
        base = "class:grid.row.alt" if n % 2 else "class:grid.row"
        lines.append(line(texts, [f"{base} {cell_style(v, kinds[i])}" for i, v in enumerate(raw)]))
    lines.append(rule("└", "┴", "┘"))

    col_x, x = [], 1
    for w in widths:
        col_x.append(x + 1)
        x += w + 3
    return lines, col_x


def to_text(grid: Grid) -> str:
    return "\n".join("".join(t for _, t in line) for line in grid.lines)
