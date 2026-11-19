"""Shell-style full-screen client.

 INSERT ● ora-docker online │ autocommit off │ oracle · tester@…
 ───────────────────────────────────────────────────────────────────
 > select * from t                      │ History  Schema  Trace  Monitor ──
 (6 rows)                               │ select * from t
                                        │ select count(*) from t
 > sel▏ect          ← input cell; ↑ moves into earlier cells to edit / re-run
 ── Result · ora-docker · select * from t (6 rows) ─────────────────────
 ┌─────┐
 │   V │ …
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.output.color_depth import ColorDepth
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.data_structures import Point
from prompt_toolkit.cursor_shapes import ModalCursorShapeConfig
from prompt_toolkit.document import Document
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import (
    Condition, emacs_insert_mode, has_focus, vi_insert_mode, vi_navigation_mode,
)
from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.keys import Keys
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.layout import (
    BufferControl, ConditionalContainer, DynamicContainer, Float, FloatContainer,
    FormattedTextControl, HSplit, Layout, ScrollablePane, VSplit, Window,
)
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import Processor, Transformation
from prompt_toolkit.layout.utils import explode_text_fragments
from prompt_toolkit.mouse_events import MouseButton, MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth

from .abbrev import Abbreviations
from .config import DATA_DIR, PANEL_TABS, Profile, UISettings
from .store import (history_stamp, history_view, load_history, load_last, load_schema, save_last, save_schema,
                    save_snapshot,
                    session_dir, take_snapshot)
from .db import (MAX_ROWS, PAGE_ROWS, changes_schema, error_text, format_table, is_connection_error, is_parse_error,
                 open_db, prepare_statement)
from . import monitor, schema
from .formatter import NotFormatted, format_sql
from .statements import statement_at
from .grid import BORDER, HEADER_LINES, Grid, build, copy_text
from .objects import blank_noise, column_refs, in_table_slot, scan, table_refs
from .oracle_hints import HINT_WEIGHT, HINTS
from .panes import neighbor
from .tab import tab_action
from .templates import NAME_PREFIX_RE, TemplateError, Templates
from .trace_view import render as render_trace
from .words import KEYWORDS, PARTIAL_RE, WORD_CHAR_RE, WORD_RE, WordModel, match_case, previous_word

PANEL_WIDTH = 32
TRACE_WIDTH = 96  # the trace tab widens the panel (at most half the screen) so a plan fits
MAX_CELLS = 200  # shell cells kept per session (oldest dropped)
MAX_HISTORY_ITEMS = 500
TAB_CANDIDATES = 15  # how many words Tab cycles through
STATS_INTERVAL = 60  # seconds between recounts of the suggestion statistics
# `!N` right before the cursor: Tab puts history entry N there
HISTORY_REF_RE = re.compile(r"(?<![\w$#!])!(\d+)$")
# a result title: the statement (cut at 60) and its row count, "(3 rows)"
RESULT_TITLE_RE = re.compile(r"(.*?)( \(\d+ rows?(?:, first \d+ shown|, more ↓)?\))", re.S)
FETCH_AHEAD = 50  # rows from the end of what is fetched at which the next page is fetched
MAX_OPEN_CURSORS = 5  # per session: large results whose cursors stay open for more rows
CONNECTIONS = 4  # per session: conn0 (the main one: schema, monitor, completion) .. conn3
PROMPT, CONTINUATION = "> ", "  "
BLOCK_END = "_ "  # last line of a multi-line cell: closes the block that "> " opens
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # a running cell's first line shows these in turn instead of "> "
SPIN_INTERVAL = 0.1

# Ctrl+Enter. Terminals only send it distinctly once asked to (xterm modifyOtherKeys,
# enabled in App.run) and prompt_toolkit maps it to plain Enter by default, so give it
# its own otherwise unused key. Both the xterm and the CSI-u (kitty, tmux) forms.
RUN_KEY = Keys.ControlF24
for _seq in ("\x1b[27;5;13~", "\x1b[13;5u"):
    ANSI_SEQUENCES[_seq] = RUN_KEY
# Alt+9 focuses the result pane, Alt+0 the prompt (ESC 9 / ESC 0, one key like Alt+1..4).
RESULTS_KEY, PROMPT_KEY = Keys.ControlF18, Keys.ControlF17
ANSI_SEQUENCES["\x1b9"] = RESULTS_KEY
ANSI_SEQUENCES["\x1b0"] = PROMPT_KEY
# Shift+Tab as tmux sends it once modified keys are asked for (xterm modifyOtherKeys form).
ANSI_SEQUENCES["\x1b[27;2;9~"] = Keys.BackTab
# Alt+Enter and Alt+1/2/3/4 arrive as ESC+key in one burst. Parsing them as single keys
# here (instead of ("escape", key) bindings) keeps a lone Esc instant in vim mode.
ALT_ENTER = Keys.ControlF23
ALT_DIGITS = [Keys.ControlF20, Keys.ControlF21, Keys.ControlF22, Keys.ControlF19, Keys.ControlF12]
ANSI_SEQUENCES["\x1b\r"] = ALT_ENTER
for _i, _key in enumerate(ALT_DIGITS):
    ANSI_SEQUENCES[f"\x1b{_i + 1}"] = _key
# Ctrl+0..3 put the cell on conn0..conn3. Only sent distinctly with modified keys asked for:
# tmux passes them on in the xterm form, other terminals may use CSI u.
CONN_KEYS = [Keys.ControlF13, Keys.ControlF14, Keys.ControlF15, Keys.ControlF16]
for _i, _key in enumerate(CONN_KEYS):
    ANSI_SEQUENCES[f"\x1b[27;5;{48 + _i}~"] = _key
    ANSI_SEQUENCES[f"\x1b[{48 + _i};5u"] = _key
MODIFY_OTHER_KEYS_ON, MODIFY_OTHER_KEYS_OFF = "\x1b[>4;1m", "\x1b[>4;0m"

# One palette for everything: soft grays, sky blue for focus/accents, muted red for trouble.
SKY, SKY_DEEP = "#8cc8ec", "#5f87af"
ORANGE = "#ff8700"  # conn0's prompt, a switch that is off, the ids in the panel
TEXT, LIGHT, DIM, FAINT = "#bcbcbc", "#dadada", "#7a7a7a", "#4a4a4a"
SELECT_BG, MENU_BG, ERROR = "#2d3d48", "#262a2d", "#d78787"
CELL_BG, STALE = "#1f2a31", "#d7af87"

STYLE = Style.from_dict({
    "ghost": "#626262",
    # SQL in the input area: a soft white of its own (not the terminal's default), so the
    # hints can be whiter than it
    "input": "#d0d0d0",
    "keyword": f"{SKY} bold",  # select, from, where …
    "relation": "#ffd7af bold",  # the tables and views a statement uses: peach
    "hint": "#ececec bold",  # optimizer hints in /*+ ... */: a touch softer than pure white
    "hint.frame": "#fadde9 bold",  # the /*+ and */ around them: as other symbols
    "symbol": "#fadde9 bold",  # , ( ) + - * / | = < > … : a pale pink, bold
    "hint.arg": "#d7af87",
    "missing": "#ff6b6b underline",  # a table, view or column the database does not have
    "relation missing": "#ff6b6b bold underline",  # a missing table stays bold, as relation names are  # what a hint names in its parentheses: aliases, indexes, @query blocks
    "prompt": f"{SKY} bold",
    "prompt.dim": FAINT,
    "prompt.stale": f"{STALE} bold",
    "prompt.running": "#ffd75f bold",
    # each connection's label on a cell, its own color: conn0 orange, conn1 green, conn2 sand, conn3 rose
    "prompt.conn0": ORANGE, "prompt.conn1": "#87d7af", "prompt.conn2": "#d7af87", "prompt.conn3": "#d787af",
    "cell.focused": f"bg:{CELL_BG}",  # an earlier cell being edited / re-run
    "note": DIM,
    "note.cancel": "#ff6b6b bold",  # ✗: a run cancelled with Ctrl-C
    "note.time": f"{FAINT} italic",  # when a run finished, beside its row count
    "error": ERROR,
    "stale": f"{STALE} italic",
    "border": FAINT,
    "selected": f"bg:{SELECT_BG}",  # vim visual selection
    # pane titles and panel tabs: no background, a thin rule instead
    "title": DIM,
    "title.focused": f"{SKY} bold",
    "tab": DIM,
    "tab.active": f"{LIGHT} bold",
    "tab.active.focused": f"bg:{SKY} #1c1c1c bold",  # the panel has focus: its tab is filled
    # lists in the panel
    "list.active": SKY,
    "list.cursor": f"bg:{SELECT_BG} {LIGHT}",
    "palette": "bg:#262626",  # Ctrl-P's command list
    "palette.group": f"bg:#262626 {DIM}",
    "palette.key": f"bg:#262626 {ORANGE}",
    "palette.match": f"bg:#262626 {SKY} bold",
    "list.note": DIM,
    "list.id": ORANGE,  # a row's number in the panel: History's run numbers, Template's ids
    "list.empty": f"{DIM} italic",
    # schema browser
    "schema.kind": f"{LIGHT} bold",
    "schema.title": f"{SKY} bold",
    "schema.back": SKY,
    "schema.invalid": ERROR,
    "schema.ddl": "",
    # top bar
    "top": TEXT,
    "top.name": f"{LIGHT} bold",
    "top.sep": FAINT,
    "top.online": SKY,
    "top.offline": ERROR,
    "top.wait": DIM,
    "top.dim": DIM,
    "top.error": ERROR,
    "top.mode": f"{SKY} bold",
    "top.off": f"{ORANGE} bold",  # the `off` of autocommit / autotrace
    # result table
    "grid.border": FAINT,
    "grid.header": f"{SKY} bold",
    "grid.row": "",
    "grid.row.alt": "bg:#1f2326",
    "grid.null": f"{DIM} italic",
    # values by type
    "grid.number": "#a6d189",
    "grid.text": "#e5c890",
    "grid.date": "#ca9ee6",
    "grid.bool": "#ef9f76",
    "grid.bytes": "#81c8be",
    # the header's N / VCH / T …: its values' color, toned down
    "grid.type": f"nobold {DIM}",
    "grid.type.number": "nobold #6f8a62",
    "grid.type.text": "nobold #94845f",
    "grid.type.date": "nobold #85699a",
    "grid.type.bytes": "nobold #5a8780",
    "grid.type.bool": "nobold #9c6a50",
    "grid.cursor": "bg:#34495a",  # the selected row: a background only, values keep their colors
    "grid.cell": f"bg:{SKY_DEEP} #ffffff bold",  # the selected cell: white on blue, the strongest mark
    # completion menu and scrollbars in the same tones
    "completion-menu": f"bg:{MENU_BG} {TEXT}",
    "completion-menu.completion.current": f"bg:{SKY_DEEP} #ffffff",
    "completion-menu.meta.completion": f"bg:{MENU_BG} {DIM}",
    "completion-menu.meta.completion.current": f"bg:{SKY_DEEP} {LIGHT}",
    # trace tab: one color per kind of plan operation
    "op": TEXT,
    "op.statement": f"{LIGHT} bold",
    "op.full": "#ff7b72 bold",  # TABLE ACCESS FULL
    "op.indexfull": "#f0a35e",  # INDEX FULL / FAST FULL / SKIP SCAN
    "op.index": "#7ee787",  # INDEX RANGE / UNIQUE SCAN: index lookup
    "op.rowid": "#56d4dd",  # TABLE ACCESS BY INDEX ROWID: key lookup
    "op.nl": "#79a8ff",  # NESTED LOOPS
    "op.hash": "#e3c35a",  # HASH JOIN
    "op.merge": "#d2a8ff",  # MERGE JOIN / SORT JOIN
    "op.sort": "#ff9bce",  # SORT, GROUP BY, WINDOW
    "op.parallel": "#a5b4fc",
    "trace.bar": SKY,
    "trace.title": f"{SKY} bold",
    "trace.subtitle": f"{LIGHT} bold",
    "trace.id": DIM,
    "trace.star": f"{STALE} bold",
    "trace.rail": FAINT,
    "trace.name": f"{LIGHT}",
    "trace.dim": DIM,
    "trace.value": TEXT,
    "trace.strong": f"{LIGHT} bold",
    "trace.key": f"{SKY} bold",
    "trace.warn": f"{STALE} bold",
    "trace.statname": TEXT,
    "trace.access": "#7ee787 bold",
    "trace.filter": "#e3c35a bold",
    "scrollbar.background": "bg:#2a2a2a",
    "scrollbar.button": f"bg:{SKY_DEEP}",
    "scrollbar.arrow": SKY,
})

STATE_LABEL = {"idle": ("not connected", "class:top.dim"), "connecting": ("connecting…", "class:top.wait"),
               "online": ("online", "class:top.online"), "offline": ("offline", "class:top.offline"),
               "failed": ("connection failed", "class:top.offline")}


# a `/` where a hint may open: right after SELECT, INSERT, UPDATE, DELETE or MERGE
HINT_OPEN_RE = re.compile(r"\b(select|insert|update|delete|merge)\s*/$", re.I)
HINT_OPEN = "*+ "  # what the `/` is completed with: `/*+ `


def hint_slot(before: str) -> str | None:
    """Inside an open `/*+` block where a hint name goes (not in a hint's parentheses): the
    case to give a hint typed from nothing, 'X' when the statement is typed in capitals, else
    'x'; None elsewhere."""
    at = before.rfind("/*+")
    if at < 0 or "*/" in before[at:]:
        return None
    body = before[at + 3:]
    if body.count("(") != body.count(")") or not (body == "" or body[-1:].isspace() or body.endswith(")")):
        return None
    lead = WORD_RE.findall(blank_noise(before[:at]))
    return "X" if lead and lead[-1].isupper() else "x"


def hint_opening(before: str, after: str) -> bool:
    """True when the `/` just typed can open a hint comment (and nothing follows it)."""
    return bool(HINT_OPEN_RE.search(blank_noise(before))) and not after.startswith("*")


def run_stamp(finished: dt.datetime, seconds: float, cancelled: bool = False) -> str:
    """`✓ 09:31:05 · 0.12s`: when a run finished (with its date when not today) and how long it
    took; `✗` instead of `✓` when it was cancelled."""
    now = dt.datetime.now()
    when = finished.strftime("%H:%M:%S") if finished.date() == now.date() else finished.strftime("%m-%d %H:%M:%S")
    took = f"{seconds * 1000:.0f}ms" if seconds < 1 else f"{seconds:.2f}s" if seconds < 60 \
        else f"{int(seconds // 60)}m {seconds % 60:04.1f}s"
    return f"{'✗' if cancelled else '✓'} {when} · {took}"


def red_cross(fragments: list) -> list:
    """The stamp's `✗` (a cancelled run) in red of its own; the rest of the stamp as it was."""
    out = []
    for st, text in fragments:
        if st == "class:note.time" and "✗" in text:
            head, _, tail = text.partition("✗")
            out += [(st, head), ("class:note.cancel", "✗"), (st, tail)]
        else:
            out.append((st, text))
    return out


class GhostText(Processor):
    """Draws the gray suggestion right after the cursor, also in the middle of a line."""

    def __init__(self, words: WordModel):
        self.words = words

    def apply_transformation(self, ti):
        doc = ti.document
        if (ti.lineno != doc.cursor_position_row or doc.text.lstrip().startswith("@")
                or not (vi_insert_mode() or emacs_insert_mode())):
            return Transformation(ti.fragments)  # no hints in a template call or vim normal mode
        if hint_opening(doc.text_before_cursor, doc.text_after_cursor):
            ghost = HINT_OPEN  # `select /` → `select /*+ `
        else:
            ghost = self.words.suggest_suffix(doc.text_before_cursor, doc.text_after_cursor)
        if not ghost:
            return Transformation(ti.fragments)
        col, n = doc.cursor_position_col, len(ghost)
        frags = explode_text_fragments(ti.fragments)
        pos = ti.source_to_display(col)
        frags = frags[:pos] + [("class:ghost", ghost)] + frags[pos:]
        return Transformation(
            frags,
            source_to_display=lambda i: i if i <= col else i + n,
            display_to_source=lambda i: i if i <= col else max(col, i - n),
        )


HINT_BLOCK_RE = re.compile(r"/\*\+.*?(\*/|\Z)", re.S)
HINT_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$#]*")
HINT_NAMES = frozenset(HINTS)
HINT_ARGS_RE = re.compile(r"\(([^()]*)\)")  # a hint's parentheses


SQL_KEYWORDS = frozenset("""
select from where and or not in is null like between exists case when then else end as on
join inner left right full outer cross natural using group by order having union all minus
intersect except distinct unique asc desc nulls first last fetch next rows row only offset
with insert into values update set delete merge matched create replace table view index
sequence synonym trigger procedure function package body type drop alter rename truncate
add modify column constraint primary key foreign references default check commit rollback
savepoint grant revoke to begin declare exception if elsif loop for while exit return
returning over partition prior connect start escape any some interval limit
""".split())
KEYWORD_RE = re.compile(r"(?<![\w$#.:@])[A-Za-z_]+(?![\w$#])")


def keyword_marks(text: str, lo: int = 0, hi: int | None = None) -> dict[int, str]:
    """SQL keywords of `text` between offsets lo and hi, outside strings and comments."""
    hi = len(text) if hi is None else hi
    plain = blank_noise(text)
    marks: dict[int, str] = {}
    for m in KEYWORD_RE.finditer(plain, max(0, lo - 40), hi):
        if m.group(0).lower() in SQL_KEYWORDS:
            for i in range(max(m.start(), lo), min(m.end(), hi)):
                marks[i] = "class:keyword"
    return marks


SYMBOL_CHARS = frozenset(",;()+-*/|=<>!%&^~?")
QUOTED_RE = re.compile(r"'(?:[^']|'')*'?|\"[^\"]*\"?")


def symbol_marks(text: str, lo: int = 0, hi: int | None = None) -> dict[int, str]:
    """Operators and punctuation (`,` `(` `+` `-` `*` `/` `|` `/*` `*/` …), not inside quotes."""
    hi = len(text) if hi is None else hi
    quoted = set()
    for m in QUOTED_RE.finditer(text):
        if m.end() >= lo and m.start() <= hi:
            quoted.update(range(m.start(), m.end()))
    return {i: "class:symbol" for i in range(lo, min(hi, len(text)))
            if text[i] in SYMBOL_CHARS and i not in quoted}


def relation_marks(text: str, lo: int = 0, hi: int | None = None) -> dict[int, str]:
    """The table and view names a statement uses (after FROM, JOIN, INTO, UPDATE)."""
    hi = len(text) if hi is None else hi
    marks: dict[int, str] = {}
    for at, name in table_refs(text):
        for i in range(max(at, lo), min(at + len(name), hi)):
            marks[i] = "class:relation"
    return marks


def sql_marks(text: str, lo: int = 0, hi: int | None = None) -> dict[int, str]:
    """Every style the SQL of `text` gets: keywords, relation names and symbols, then hint
    blocks over them."""
    marks = keyword_marks(text, lo, hi)
    marks.update(relation_marks(text, lo, hi))
    marks.update(symbol_marks(text, lo, hi))
    hints = hint_marks(text, lo, hi)
    for i, style in hints.items():
        if not (style == "class:hint.arg" and marks.get(i) == "class:symbol"):
            marks[i] = style
    return marks


def hint_marks(text: str, lo: int = 0, hi: int | None = None) -> dict[int, str]:
    """Styles for the hint blocks of `text` between offsets lo and hi: names near-white, what
    their parentheses name (table aliases, index names, `@query_block`s) sand, `/*+ */` dim."""
    hi = len(text) if hi is None else hi
    marks: dict[int, str] = {}

    def mark(at: int, length: int, style: str) -> None:
        for i in range(max(at, lo), min(at + length, hi)):
            marks[i] = style

    for block in HINT_BLOCK_RE.finditer(text):
        if block.end() < lo or block.start() > hi:
            continue
        body, base = block.group(0), block.start()
        mark(base, 3, "class:hint.frame")  # /*+
        if body.endswith("*/") and len(body) >= 5:
            mark(base + len(body) - 2, 2, "class:hint.frame")
        for word in HINT_WORD_RE.finditer(body):
            if word.group(0).lower() in HINT_NAMES:
                mark(base + word.start(), len(word.group(0)), "class:hint")
        for args in HINT_ARGS_RE.finditer(body):  # full(e) index(e emp_ix) leading(@sel$1 d e)
            for name in HINT_WORD_RE.finditer(args.group(1)):
                mark(base + args.start(1) + name.start(), len(name.group(0)), "class:hint.arg")
            for qb in re.finditer(r"@", args.group(1)):
                mark(base + args.start(1) + qb.start(), 1, "class:hint.arg")
    return marks


def highlighted(text: str, base: str = "class:input") -> list[tuple[str, str]]:
    """`text` as fragments, styled as in the input area (for the history list)."""
    marks = sql_marks(text)
    out: list[tuple[str, str]] = []
    for i, ch in enumerate(text):
        style = f"{base} {marks[i]}" if i in marks else base
        if out and out[-1][0] == style:
            out[-1] = (style, out[-1][1] + ch)
        else:
            out.append((style, ch))
    return out


class SqlHighlight(Processor):
    """The input area's keywords and hint blocks (`/*+ ... */` may span lines), by sql_marks."""

    def apply_transformation(self, ti):
        doc = ti.document
        line_start = doc.translate_row_col_to_index(ti.lineno, 0)
        marks = sql_marks(doc.text, line_start, line_start + len(doc.lines[ti.lineno]))
        if not marks:
            return Transformation(ti.fragments)
        frags = explode_text_fragments(ti.fragments)
        for i in range(len(frags)):
            if line_start + i in marks:
                frags[i] = (f"{frags[i][0]} {marks[line_start + i]}", frags[i][1], *frags[i][2:])
        return Transformation(frags)


class MissingObjects(Processor):
    """A red underline under table, view and column names the database does not have."""

    def __init__(self, known: Callable[[], set[str] | None],
                 columns: Callable[[list[str]], dict[str, frozenset | None] | None]):
        self.known = known
        self.columns = columns  # {table: its columns or None}, or None while they are loading

    def apply_transformation(self, ti):
        names = self.known()
        if not names:  # not connected yet: nothing to check against
            return Transformation(ti.fragments)
        doc = ti.document
        line_start = doc.translate_row_col_to_index(ti.lineno, 0)
        line_end = line_start + len(doc.lines[ti.lineno])
        typing = get_app().layout.current_buffer is ti.buffer_control.buffer
        marks = set()
        def mark(at: int, length: int) -> None:
            end = at + length
            if end < line_start or at > line_end or (typing and end == doc.cursor_position):
                return  # another line, or still being typed
            marks.update(range(max(at, line_start), min(end, line_end)))

        for at, name in table_refs(doc.text):
            if name.lower() not in names:
                mark(at, len(name))
        found = column_refs(doc.text)
        if found:
            refs, tables = found
            present = [t for t in tables if t.lower() in names]
            cols = self.columns(present) if present else None
            if cols is not None:
                every = None if len(present) < len(tables) or any(cols[t] is None for t in present) \
                    else frozenset().union(*(cols[t] for t in present))
                for ref in refs:
                    have = cols.get(ref.table) if ref.table is not None else every
                    if have is not None and ref.name.lower() not in have:
                        mark(ref.at, len(ref.name))
        if not marks:
            return Transformation(ti.fragments)
        frags = explode_text_fragments(ti.fragments)
        for i in range(len(frags)):
            if line_start + i in marks:
                frags[i] = (f"{frags[i][0]} class:missing", frags[i][1], *frags[i][2:])
        return Transformation(frags)


class WordCompleter(Completer):
    def __init__(self, words: WordModel, templates: Templates):
        self.words, self.templates = words, templates

    def get_completions(self, document, complete_event):
        t = NAME_PREFIX_RE.fullmatch(document.text_before_cursor)
        seg = self.templates.call_at(document.text_before_cursor, document.text_after_cursor)
        inline = NAME_PREFIX_RE.fullmatch(seg) if seg is not None else None
        if t or inline:  # "@" / "@na" (also inside a line): template names, their body as a hint
            prefix = (t or inline).group(1) or ""
            for name in self.templates.names(prefix):
                yield Completion(name, start_position=-len(prefix),
                                 display_meta=self.templates.describe(name)[:60])
            return
        m = PARTIAL_RE.search(document.text_before_cursor)
        if not m:
            return
        prefix = m.group(0)
        for word in self.words.candidates(prefix, before=document.text_before_cursor):
            yield Completion(word, start_position=-len(prefix))


def clicked(fn: Callable[[], None]):
    def handler(mouse_event):
        if mouse_event.event_type != MouseEventType.MOUSE_UP:
            return NotImplemented
        fn()
    return handler


class ScrollingControl(FormattedTextControl):
    """A list whose cursor follows the mouse wheel (the window scrolls by moving it)."""

    def __init__(self, *args, move: Callable[[int], None], on_mouse=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._move = move
        self._on_mouse = on_mouse  # asked first: a drag goes on past the text's end

    def mouse_handler(self, mouse_event):
        if self._on_mouse is not None:
            handled = self._on_mouse(mouse_event)
            if handled is not NotImplemented:
                return handled
        return super().mouse_handler(mouse_event)

    def move_cursor_down(self) -> None:
        self._move(1)

    def move_cursor_up(self) -> None:
        self._move(-1)


@dataclass
class PanelTab:
    name: str  # config value: history | schema | trace | monitor
    label: str
    items: Callable[[], list[tuple[str, str, str]]]  # (label, style, dim note)
    on_pick: Callable[[int], None]
    keys: dict[str, Callable[[], None]] = field(default_factory=dict)
    cursor: int = 0
    width: Callable[[], int] | None = None  # the panel's width while this tab shows


class TabbedPanel:
    """One panel that shows one of several lists; ←/→, Tab / Shift-Tab, 1-9 switch lists."""

    def __init__(self, tabs: list[PanelTab], current: str, width: Callable[[], int] | None = None):
        self.tabs = tabs
        self._width = width
        self.index = [t.name for t in tabs].index(current)
        kb = KeyBindings()
        kb.add("up")(kb.add("k")(lambda e: self._move(-1)))
        kb.add("down")(kb.add("j")(lambda e: self._move(1)))
        kb.add("g")(kb.add("home")(lambda e: self._move(-10**9)))
        kb.add("G")(kb.add("end")(lambda e: self._move(10**9)))
        kb.add("c-u")(lambda e: self._scroll(-max(1, self._page() // 2)))
        kb.add("c-d")(lambda e: self._scroll(max(1, self._page() // 2)))
        kb.add("enter")(lambda e: self._pick())
        kb.add("right")(kb.add("l")(kb.add("tab")(lambda e: self.select((self.index + 1) % len(self.tabs)))))
        kb.add("left")(kb.add("h")(kb.add("s-tab")(lambda e: self.select((self.index - 1) % len(self.tabs)))))
        for i in range(len(tabs)):
            kb.add(str(i + 1))(lambda e, i=i: self.select(i))
        for key in {k for t in tabs for k in t.keys}:
            kb.add(key)(lambda e, key=key: self.tab.keys.get(key, lambda: None)())
        self.window = Window(
            ScrollingControl(self._fragments, focusable=True, show_cursor=False, key_bindings=kb,
                             get_cursor_position=lambda: Point(0, self.tab.cursor), move=self._move),
            right_margins=[ScrollbarMargin(display_arrows=False)])
        header = Window(FormattedTextControl(self._header), height=1, char="─", style="class:border")
        self.container = HSplit([header, self.window], width=lambda: Dimension.exact(self.width()))

    def width(self) -> int:
        if self._width is not None:
            return self._width()
        return self.tab.width() if self.tab.width else PANEL_WIDTH

    @property
    def tab(self) -> PanelTab:
        return self.tabs[self.index]

    def select(self, i: int) -> None:
        self.index = i
        get_app().invalidate()

    def _move(self, delta: int) -> None:
        n = len(self.tab.items())
        self.tab.cursor = max(0, min(n - 1, self.tab.cursor + delta)) if n else 0

    def _page(self) -> int:
        info = self.window.render_info
        return max(1, (info.window_height if info else 10) - 1)

    def page(self, direction: int) -> None:
        """PageUp (-1) / PageDown (1), from whichever pane has focus."""
        self._scroll(direction * self._page())

    def _scroll(self, delta: int) -> None:
        """Move the view and the cursor together, like a pager (a plan reads top to bottom)."""
        self._move(delta)
        info = self.window.render_info
        if info is not None:
            top = max(0, len(self.tab.items()) - info.window_height)
            self.window.vertical_scroll = max(0, min(top, self.window.vertical_scroll + delta))

    def _pick(self) -> None:
        if self.tab.cursor < len(self.tab.items()):
            self.tab.on_pick(self.tab.cursor)

    def _header(self):
        focused = get_app().layout.current_window is self.window
        out = []
        for i, t in enumerate(self.tabs):
            style = ("class:tab.active.focused" if focused else "class:tab.active") if i == self.index else "class:tab"
            out.append((style, f" {t.label} ", clicked(lambda i=i: self.select(i))))  # spaces inside: the fill pads the label
        return out

    def _fragments(self):
        tab, entries = self.tab, self.tab.items()
        if not entries:
            return [("class:list.empty", " (none)")]
        tab.cursor = min(tab.cursor, len(entries) - 1)
        focused = get_app().layout.current_window is self.window
        out = []
        for i, (label, style, note) in enumerate(entries):
            cur = " class:list.cursor" if focused and i == tab.cursor else ""
            pick = clicked(lambda i=i: self._click(i))
            note = f" {note}" if note else ""
            # a label is plain text, or (style, text) fragments of its own (the trace tab)
            frags = [(style, label)] if isinstance(label, str) else [(f"{style} {st}", t) for st, t in label]
            used = sum(get_cwidth(t) for _, t in frags)
            pad = max(0, self.width() - 1 - used - get_cwidth(note) - 2)  # 1: scrollbar
            # the selection's background first, so a label's own colors (hints) still show on it
            out += [(style + cur, " ", pick)] + [(f"{cur} {st}", t, pick) for st, t in frags]
            out += [(style + cur, " " * pad, pick), ("class:list.note" + cur, note + " ", pick), ("", "\n")]
        return out[:-1]

    def _click(self, i: int) -> None:
        self.tab.cursor = i
        self.tab.on_pick(i)


@dataclass(frozen=True)
class Command:
    """A palette entry: what it does, and the keys that do it (fed in when picked)."""
    group: str
    label: str
    shortcut: str  # as shown
    keys: tuple  # the key presses to replay
    where: str = "any"  # any | cell | cell-normal | results | tab:<name>


def palette_match(query: str, c: Command) -> int | None:
    """A rank for `c` (lower is better), None when it does not match: every word of the query
    must appear in the label, the group or the shortcut."""
    hay = f"{c.label} {c.group} {c.shortcut}".lower()
    words = query.lower().split()
    if not all(w in hay for w in words):
        return None
    return 0 if not words or c.label.lower().startswith(words[0]) else 1


class CommandPalette:
    """Ctrl-P: every command with its shortcut; type to search, ↑/↓ to choose, Enter to run."""

    ROWS = 16

    def __init__(self, commands: list[Command], run: Callable[[Command], None]):
        self.commands, self._run = commands, run
        self.open = False
        self.index = 0
        self.buffer = Buffer(multiline=False, on_text_changed=lambda b: setattr(self, "index", 0))
        kb = KeyBindings()
        kb.add("up")(kb.add("c-p")(kb.add("c-k")(lambda e: self._move(-1))))
        kb.add("down")(kb.add("c-n")(kb.add("c-j")(lambda e: self._move(1))))
        kb.add("pageup")(lambda e: self._move(-self.ROWS))
        kb.add("pagedown")(lambda e: self._move(self.ROWS))
        kb.add("enter")(lambda e: self.pick())
        kb.add("escape", eager=True)(lambda e: self.close())
        kb.add("c-c")(lambda e: self.close())
        self.search = Window(BufferControl(buffer=self.buffer, key_bindings=kb), height=1, style="class:palette",
                             get_line_prefix=lambda n, w: [("class:palette.key", " > ")])
        self.on_close: Callable[[], None] = lambda: None
        body = HSplit([
            self.search,
            Window(height=1, char="─", style="class:palette.group"),
            Window(FormattedTextControl(self._fragments), style="class:palette",
                   height=lambda: Dimension.exact(max(1, min(self.ROWS, len(self.matches()))))),
        ], width=Dimension.exact(72), style="class:palette")
        self.container = ConditionalContainer(body, filter=Condition(lambda: self.open))

    def matches(self) -> list[Command]:
        q = self.buffer.text
        ranked = [(r, i, c) for i, c in enumerate(self.commands) if (r := palette_match(q, c)) is not None]
        return [c for _, _, c in sorted(ranked)]

    def _move(self, delta: int) -> None:
        n = len(self.matches())
        self.index = max(0, min(n - 1, self.index + delta)) if n else 0

    def pick(self, i: int | None = None) -> None:
        found = self.matches()
        i = self.index if i is None else i
        if 0 <= i < len(found):
            self.close()
            self._run(found[i])

    def close(self) -> None:
        if self.open:
            self.open = False
            self.on_close()

    def _fragments(self):
        found = self.matches()
        if not found:
            return [("class:palette.group", "  no command matches")]
        top = max(0, min(self.index - self.ROWS // 2, len(found) - self.ROWS))
        words = self.buffer.text.lower().split()
        out = []
        for i, c in enumerate(found[top:top + self.ROWS], start=top):
            cur = " class:list.cursor" if i == self.index else ""
            pick = clicked(lambda i=i: self.pick(i))
            label = f"{c.group}: {c.label}"
            used = 2 + get_cwidth(label) + get_cwidth(c.shortcut) + 1
            out.append(("class:palette" + cur, "  ", pick))
            out.append(("class:palette.group" + cur, f"{c.group}: ", pick))
            for frag_style, text in self._marked(c.label, words):
                out.append((frag_style + cur, text, pick))
            out.append(("class:palette" + cur, " " * max(1, 72 - used), pick))
            out.append(("class:palette.key" + cur, c.shortcut + " ", pick))
            out.append(("", "\n"))
        return out[:-1]

    @staticmethod
    def _marked(label: str, words: list[str]) -> list[tuple[str, str]]:
        """The label with the searched words in bold sky blue."""
        marks = [False] * len(label)
        low = label.lower()
        for w in words:
            at = low.find(w)
            while w and at >= 0:
                for k in range(at, at + len(w)):
                    marks[k] = True
                at = low.find(w, at + len(w))
        out: list[tuple[str, str]] = []
        for ch, m in zip(label, marks):
            st = "class:palette.match" if m else "class:palette"
            if out and out[-1][0] == st:
                out[-1] = (st, out[-1][1] + ch)
            else:
                out.append((st, ch))
        return out


class ResultGrid:
    """Bottom pane: a result as a box table; j/k rows, h/l columns when focused."""

    EMPTY = ("class:list.empty", " Query results show up here as a table")

    def __init__(self):
        self.grid: Grid | None = None
        self.on_copy: Callable[[str, str], None] = lambda text, what: None  # set by the app
        self.on_near_end: Callable[[], None] = lambda: None  # set by the app: fetch more rows
        self._drag: tuple[int, int, int] | None = None  # (column, where the drag began, its width then)
        self.placeholder = self.EMPTY
        self.row = self.col = 0
        kb = KeyBindings()
        kb.add("up")(kb.add("k")(lambda e: self._move(-1, 0)))
        kb.add("down")(kb.add("j")(lambda e: self._move(1, 0)))
        kb.add("left")(kb.add("h")(lambda e: self._move(0, -1)))
        kb.add("right")(kb.add("l")(lambda e: self._move(0, 1)))
        kb.add("c-u")(lambda e: self._scroll(-max(1, self._page() // 2)))
        kb.add("c-d")(lambda e: self._scroll(max(1, self._page() // 2)))
        kb.add("g")(kb.add("home")(lambda e: self._move(-10**9, 0)))
        kb.add("G")(kb.add("end")(lambda e: self._move(10**9, 0)))
        kb.add("s")(lambda e: self.sort(self.col))  # sort by the selected column, as a header click
        kb.add("y")(lambda e: self.copy(row=False))  # the selected cell
        kb.add("Y")(lambda e: self.copy(row=True))  # the selected row, tab-separated
        kb.add("0")(lambda e: self._move(0, -10**9))
        kb.add("$")(lambda e: self._move(0, 10**9))
        self.window = Window(
            ScrollingControl(self._fragments, focusable=True, show_cursor=False, key_bindings=kb,
                             get_cursor_position=self._cursor, move=lambda d: self._move(d, 0),
                             on_mouse=self._mouse),
            wrap_lines=False, height=self._height,
            right_margins=[ScrollbarMargin(display_arrows=True)])

    @staticmethod
    def _height() -> Dimension:
        """About 40% of the screen, however many rows the result has (a long one scrolls)."""
        rows = max(5, get_app().output.get_size().rows * 2 // 5)
        return Dimension(min=5, preferred=rows, max=rows)

    def _mouse(self, ev):
        """Drag a column's right border `│` to change its width."""
        grid, x = self.grid, ev.position.x
        if grid is None:
            return NotImplemented
        if ev.event_type == MouseEventType.MOUSE_DOWN and ev.button == MouseButton.LEFT:
            if ev.position.y >= len(grid.lines):
                return NotImplemented
            hit = [i for i in range(len(grid.widths)) if abs(grid.border_x(i) - x) <= 1]
            if not hit:
                return NotImplemented
            i = min(hit, key=lambda i: abs(grid.border_x(i) - x))
            self._drag = (i, x, grid.widths[i])
            return None
        if self._drag is not None:
            i, start_x, start_w = self._drag
            if ev.event_type == MouseEventType.MOUSE_MOVE and ev.button in (MouseButton.LEFT, MouseButton.UNKNOWN):
                if start_w + x - start_x != grid.widths[i]:
                    grid.resize(i, start_w + x - start_x)
                    get_app().invalidate()
                return None
            if ev.event_type == MouseEventType.MOUSE_UP or ev.button == MouseButton.NONE:
                self._drag = None
                return None
        # a click on a column's header cell sorts by it
        if ev.event_type == MouseEventType.MOUSE_UP and ev.position.y == 1:
            i = grid.column_at(x)
            if i is not None:
                self.sort(i)
                return None
        return NotImplemented

    def copy(self, row: bool) -> None:
        if self.grid is None or not self.grid.rows:
            return
        if row:
            values = [self.grid.value(self.row, c) for c in range(len(self.grid.widths))]
            text = "\t".join(copy_text(v).replace("\t", " ").replace("\n", " ") for v in values)
            what = f"row {self.row + 1}"
        else:
            text = copy_text(self.grid.value(self.row, self.col))
            what = "cell"
        self.on_copy(text, what)

    def sort(self, i: int) -> None:
        if self.grid is not None:
            self.grid.sort_by(i)
            self.row, self.col = 0, i
            get_app().invalidate()

    def show(self, grid: Grid | None, placeholder: tuple[str, str] | None = None) -> None:
        self._drag = None
        self.grid, self.row, self.col = grid, 0, 0
        self.placeholder = placeholder or self.EMPTY

    def _page(self) -> int:
        info = self.window.render_info
        return max(1, (info.window_height if info else 10) - HEADER_LINES - 1)

    def page(self, direction: int) -> None:
        """Ctrl+PageUp (-1) / Ctrl+PageDown (1), from whichever pane has focus."""
        self._scroll(direction * self._page())

    def _scroll(self, delta: int) -> None:
        """Move the view and the row cursor together, like a pager."""
        self._move(delta, 0)
        info = self.window.render_info
        if self.grid and info is not None:
            top = max(0, len(self.grid.lines) - info.window_height)
            self.window.vertical_scroll = max(0, min(top, self.window.vertical_scroll + delta))

    def _move(self, drow: int, dcol: int) -> None:
        if self.grid:
            self.row = max(0, min(self.grid.rows - 1, self.row + drow))
            self.col = max(0, min(len(self.grid.col_x) - 1, self.col + dcol))
            if drow > 0 and self.row >= self.grid.rows - FETCH_AHEAD:
                self.on_near_end()  # a large result: fetch its next page

    def _cursor(self) -> Point:
        if not self.grid:
            return Point(0, 0)
        # keep the header in view on the first rows; the window follows the cursor
        y = HEADER_LINES + self.row if self.row else 0
        return Point(self.grid.col_x[self.col] if self.grid.col_x else 0, y)

    def _fragments(self):
        if not self.grid:
            style, text = self.placeholder
            return [(style, " " + text.replace("\n", "\n "))]
        focused = get_app().layout.current_window is self.window
        cursor_line = HEADER_LINES + self.row if focused and self.grid.rows else -1
        out = []
        for i, line in enumerate(self.grid.lines):
            if i == cursor_line:
                # the row gets a background; the selected cell, a stronger one of its own
                # (a line is │ cell │ cell │ …: cell n is fragment 1 + 2n)
                cell_at = 1 + 2 * self.col
                line = [(st if st == BORDER else st + (" class:grid.cell" if k == cell_at else " class:grid.cursor"), t)
                        for k, (st, t) in enumerate(line)]
            out.extend(line)
            out.append(("", "\n"))
        return out[:-1]


class Cell:
    """One statement in the shell: an editable input plus the summary of its last run.

    The bottom cell of a session is the fresh input. Earlier cells stay editable: move
    into them with ↑/↓, edit (the result is then marked stale) and run them again.
    A note cell has no input, only a message (connection events and the like).
    """

    def __init__(self, app: "App", session: "Session", note: tuple[str, str] | None = None):
        self.app, self.session = app, session
        self.ran_text: str | None = None  # input as it was last executed
        self.output: list[tuple[str, str]] = [note] if note else []
        self.grid: Grid | None = None
        self.trace: list[str] | None = None  # autotrace lines of the last run, if it was traced
        self.data: tuple = ([], [], None)  # the result's columns, rows and column types, to save
        self.pager = None  # the open cursor of a large result, for its further pages
        self.fetching = False
        self.result_title = ""
        self.version = 0  # bumped on every run, so the result pane knows to refresh
        self.running = False
        self.conn = None  # the connection running it right now, to cancel it on
        self.conn_index = 0  # which of the session's connections it runs on (conn0..conn3; Ctrl+digit)
        self.pager_index = 0  # the connection its open result cursor (pager) is on
        self.cancelled = False  # Ctrl-C asked to stop it
        self.waiting = False  # queued behind another statement on its connection
        self.buffer: Buffer | None = None
        self.window: Window | None = None
        parts = []
        if note is None:
            self.buffer = Buffer(multiline=True, completer=app.completer, complete_while_typing=False,
                                 on_text_changed=lambda buf: app.drop_if_empty(self))
            self.window = Window(
                BufferControl(buffer=self.buffer, input_processors=[SqlHighlight(),
                                                                MissingObjects(lambda: self.session.objects,
                                                                               lambda t: app.columns_for(self.session, t)),
                                                                GhostText(app.words)]),
                dont_extend_height=True, wrap_lines=True, get_line_prefix=self._prefix,
                style=self._style)
            app.cell_of[self.window] = self
            parts.append(self.window)
        parts.append(ConditionalContainer(
            Window(FormattedTextControl(self._output_fragments), dont_extend_height=True, wrap_lines=True),
            filter=Condition(lambda: bool(self._output_fragments()))))
        parts.append(ConditionalContainer(Window(height=1), filter=Condition(lambda: not self.is_input)))
        self.container = HSplit(parts)

    @property
    def is_input(self) -> bool:
        return self is self.session.input_cell

    @property
    def has_run(self) -> bool:
        return self.ran_text is not None

    @property
    def stale(self) -> bool:
        """Edited since it last ran: its result no longer matches the text."""
        return self.has_run and self.buffer is not None and self.buffer.text.strip() != self.ran_text.strip()

    def focused(self) -> bool:
        return self.window is not None and get_app().layout.current_window is self.window

    def _style(self) -> str:
        return "class:input class:cell.focused" if self.focused() and not self.is_input else "class:input"

    def _prefix(self, lineno: int, wrap_count: int):
        """`conn0> ` on the first line (a spinner for `>` while it runs), `_ ` under it on the last."""
        style = "class:prompt.stale" if self.stale else "class:prompt" if self.focused() else "class:prompt.dim"
        label = f"conn{self.conn_index}"
        pad = " " * len(label)
        if wrap_count:  # a long line carried onto the next row
            return [(style, pad + CONTINUATION)]
        if lineno == 0:
            head = (f"{style} class:prompt.conn{self.conn_index}", label)
            if self.running:  # still running: a spinner where ">" goes
                frame = SPINNER[int(time.monotonic() / SPIN_INTERVAL) % len(SPINNER)]
                return [head, ("class:prompt.running", frame + " ")]
            return [head, (style, PROMPT)]
        last = self.buffer.document.line_count - 1
        return [(style, pad + (BLOCK_END if lineno == last else CONTINUATION))]

    def _output_fragments(self):
        if self.running:
            return [("class:note", f"waiting for conn{self.conn_index}…" if self.waiting else "running…")]
        if self.stale:  # edited since it ran: the outcome goes from the shell; the result pane says (stale)
            return []
        return self.output


KIND_LABELS = {"TABLE": "Tables", "VIEW": "Views", "MATERIALIZED VIEW": "Materialized views",
               "INDEX": "Indexes", "SEQUENCE": "Sequences", "SYNONYM": "Synonyms", "TRIGGER": "Triggers",
               "PROCEDURE": "Procedures", "FUNCTION": "Functions", "PACKAGE": "Packages", "TYPE": "Types"}
DDL_MARK = "class:schema.ddl"  # every line of a DDL section starts with this fragment


@dataclass
class Browser:
    """Where the schema tab is: the kinds, a kind's objects, or one object's details."""
    level: str = "kinds"  # kinds | objects | detail
    kinds: list | None = None
    kind: str = ""
    objects: list = field(default_factory=list)
    name: str = ""
    lines: list = field(default_factory=list)  # the details, drawn
    ddl: str = ""
    loading: bool = False
    cursors: dict = field(default_factory=dict)  # each level's selection, to come back to


@dataclass
class Watch:
    """The monitor tab: its last snapshot, drawn, and when it was taken."""
    lines: list = field(default_factory=list)
    when: dt.datetime | None = None
    loading: bool = False
    paused: bool = False


def schema_lines(sections) -> list:
    """An object's details as panel lines: each section a title and a small boxed table."""
    out: list = []
    for sec in sections:
        out.append([("class:trace.bar", "▌"), ("class:trace.title", f" {sec.title}")])
        if sec.error:
            out.append([("class:error", f"  {sec.error}")])
        elif not sec.rows:
            out.append([("class:list.empty", "  (none)")])
        else:
            grid = build([c.upper() for c in sec.columns], sec.rows)
            out += [[("", "  ")] + line for line in grid.lines]
        out.append([])
    return out


def schema_lines_without_ddl(lines: list) -> list:
    return [l for l in lines if not (l and l[0][0] == DDL_MARK)]


def ddl_lines(ddl: str) -> list:
    """The DDL, highlighted as SQL, each line marked so a reload can replace it."""
    head = [(DDL_MARK, ""), ("class:trace.bar", "▌"), ("class:trace.title", " DDL"),
            ("class:list.note", "  y copies it")]
    body = [[(DDL_MARK, "  ")] + highlighted(line.expandtabs(4).rstrip())
            for line in (ddl or "(none)").splitlines()]
    return [head] + body


@dataclass
class Session:
    """The connection's state: one per terminal, its files under .dbclidata/sessions/<name>/."""
    profile: Profile
    db: object | None = None
    relations: list[tuple[str, str]] = field(default_factory=list)
    objects: set[str] | None = None  # table/view names to check statements against (lower case)
    objects_sorted: list[str] = field(default_factory=list)  # the same, sorted: offered after FROM
    columns: dict[str, frozenset | None] = field(default_factory=dict)  # table -> its columns, as loaded
    pagers: list = field(default_factory=list)  # cells whose large result keeps its cursor open
    browser: "Browser" = field(default_factory=lambda: Browser())  # the schema tab's place
    watch: "Watch" = field(default_factory=lambda: Watch())  # the monitor tab's last snapshot
    catalog_when: dt.datetime | None = None  # when Ctrl-O in the schema tab last read the catalog
    catalog_stale: bool = False  # a DDL ran since: the catalog may be out of date
    state: str = "idle"  # idle | connecting | online | offline | failed
    running: bool = False
    autotrace: bool = True  # on from the start; F8 / click: plan and statistics in the trace tab (Oracle)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # one DB call at a time
    connecting: asyncio.Task | None = None
    cells: list[Cell] = field(default_factory=list)  # oldest first, input cell last
    input_cell: Cell | None = None
    edit_cell: Cell | None = None  # last focused editable cell (panel inserts go here)
    last_run: Cell | None = None  # most recently executed cell
    shown: Cell | None = None  # cell whose result is in the bottom pane
    stack: HSplit | None = None
    container: ScrollablePane | None = None
    folder: Path | None = None  # .dbclidata/sessions/<name>
    runs: list[tuple[dt.datetime | None, str]] = field(default_factory=list)  # every run; run N is runs[N-1]
    history: list[tuple[int, dt.datetime | None, str]] = field(default_factory=list)  # the History tab's rows
    file_history: FileHistory | None = None
    extra: list = field(default_factory=lambda: [None] * (CONNECTIONS - 1))  # conn1..conn3, opened on first use
    extra_locks: list = field(default_factory=lambda: [asyncio.Lock() for _ in range(CONNECTIONS - 1)])
    restore: dict | None = None  # where F1's snapshot left the focus, applied once the app runs


class App:
    def __init__(self, profiles: list[Profile], abbrevs: Abbreviations, ui: UISettings,
                 templates: Templates):
        DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)  # also holds passwords
        self.profiles = profiles
        self.abbrevs = abbrevs
        self.templates = templates
        self.words = WordModel(DATA_DIR / "words.db")
        self.completer = WordCompleter(self.words, self.templates)
        self.words.context = self._completion_context
        self.show_panel = ui.show_panel
        self.vi = ui.editing_mode == "vi"
        self.message, self.error = "", False
        self._tab_cycle: dict | None = None  # the word Tab is cycling through, see _cycle_tab
        self.cell_of: dict[Window, Cell] = {}
        self._shown_key: tuple | None = None

        self.sessions = {p.name: self._new_session(p) for p in profiles}
        self.active = profiles[0].name

        # -- shell (top left): the active session's cells, scrolled to the focused one --
        shell = DynamicContainer(lambda: self._session.container)

        # -- panel (top right): history / schema / trace / monitor, one at a time --
        tabs = {
            "history": PanelTab("history", "History", self._history_items, self._recall),
            "schema": PanelTab("schema", "Schema", self._schema_items, self._schema_pick,
                               keys={"backspace": self._schema_back, "i": self._schema_insert,
                                     "d": self._schema_ddl, "y": self._schema_copy, "c-o": self._schema_collect}),
            "trace": PanelTab("trace", "Trace", self._trace_items, lambda i: None,
                              width=self._trace_width),
            "monitor": PanelTab("monitor", "Monitor", self._monitor_items, lambda i: None,
                                keys={"c-o": self._monitor_now}),
            "template": PanelTab("template", "Template", self._template_items, self._template_edit,
                                 keys={"a": self._template_add, "e": self._template_edit_current,
                                       "r": self._template_rename, "d": self._template_delete,
                                       "i": self._template_insert, "y": self._template_copy}),
        }
        # every tab as wide as the trace tab needs, so switching tabs never moves the shell;
        # the monitor's wide tables get half the screen
        self.panel = TabbedPanel([tabs[n] for n in PANEL_TABS], ui.panel, width=self._panel_width)
        self._tpl_delete: int | None = None  # the template `d` asked about (a second `d` deletes)
        self._ask: dict | None = None  # the one-line question under the panel (a template's alias)
        self.ask_buffer = Buffer(multiline=False)
        ask_kb = KeyBindings()
        ask_kb.add("enter")(lambda e: self._ask_done(self.ask_buffer.text.strip()))
        ask_kb.add("escape", eager=True)(lambda e: self._ask_done(None))
        ask_kb.add("c-c")(lambda e: self._ask_done(None))
        ask_kb.add("c-u")(lambda e: self.ask_buffer.delete_before_cursor(self.ask_buffer.cursor_position))
        self.ask_window = Window(
            BufferControl(buffer=self.ask_buffer, key_bindings=ask_kb), height=1, style="class:input",
            get_line_prefix=lambda n, w: [("class:list.note", f" {self._ask['label']} " if self._ask else " ")])
        # the template editor: the Template tab's place while a body is edited (vi keys, `:w` …)
        self._tpl_edit: dict | None = None  # {"id": id or None for a new one, "alias", "saved": body}
        self.editor_buffer = Buffer(multiline=True, completer=self.completer, complete_while_typing=False)
        ed_kb = KeyBindings()
        ed_kb.add(":", filter=vi_navigation_mode)(lambda e: self._ask_for(":", "", lambda a: None, self._editor_command))
        ed_kb.add("Z", "Z", filter=vi_navigation_mode)(lambda e: self._editor_command("wq"))
        ed_kb.add("Z", "Q", filter=vi_navigation_mode)(lambda e: self._editor_command("q!"))
        ed_kb.add("c-s")(lambda e: self._editor_command("w"))
        self.editor_window = Window(
            BufferControl(buffer=self.editor_buffer, key_bindings=ed_kb, input_processors=[SqlHighlight()]),
            wrap_lines=True, style="class:input",
            get_line_prefix=lambda n, w: [("class:list.note", f"{n + 1:>3} " if not w else "    ")])
        editing = Condition(lambda: self._tpl_edit is not None and self.panel.tab.name == "template")
        editor = HSplit([Window(FormattedTextControl(self._editor_header), height=1, char="─", style="class:border"),
                         self.editor_window], width=lambda: Dimension.exact(self.panel.width()))
        top = VSplit([
            shell,
            ConditionalContainer(
                VSplit([Window(width=1, char="│", style="class:border"),
                        HSplit([ConditionalContainer(self.panel.container, filter=~editing),
                                ConditionalContainer(editor, filter=editing),
                                ConditionalContainer(self.ask_window,
                                                     filter=Condition(lambda: self._ask is not None))])]),
                filter=Condition(lambda: self.show_panel)),
        ], height=Dimension(weight=3))

        # -- results (bottom, full width, ~40% of the height) --
        self.results = ResultGrid()
        self.results.on_copy = self.copy_to_clipboard
        self.results.on_near_end = self.fetch_more
        results_title = Window(FormattedTextControl(self._results_title), height=1, char="─",
                               style="class:border")

        # -- top bar: connection and online state --
        top_bar = HSplit([Window(FormattedTextControl(self._top_bar), height=1),
                          Window(height=1, char="─", style="class:border")])

        self.fixed_panes = [self.panel.window, self.results.window]
        self.palette = CommandPalette(self._commands(), self._run_command)
        self.palette.on_close = self._palette_closed
        self._palette_back = None  # the window focused when the palette opened
        root = FloatContainer(HSplit([top_bar, top, results_title, self.results.window]), floats=[
            Float(top=2, content=self.palette.container),
            Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=10)),
        ])
        first = self._session.input_cell.window
        # true color where the terminal says it has it (COLORTERM), so the soft shades show as
        # written; otherwise prompt_toolkit's 256 colors, which round them to the nearest entry
        depth = ColorDepth.DEPTH_24_BIT if os.environ.get("COLORTERM") in ("truecolor", "24bit") else None
        self.app = Application(layout=Layout(root, focused_element=first), color_depth=depth,
                               key_bindings=self._global_bindings(), style=STYLE,
                               full_screen=True, mouse_support=True,
                               editing_mode=EditingMode.VI if self.vi else EditingMode.EMACS,
                               cursor=ModalCursorShapeConfig(),  # block in normal, bar in insert
                               before_render=self._sync_results)
        self.app.ttimeoutlen = 0.05  # Esc to normal mode without a noticeable delay

    @property
    def _session(self) -> Session:
        return self.sessions[self.active]

    # -- Ctrl-P: the command palette ------------------------------------------
    def _commands(self) -> list[Command]:
        C = Command
        tabs = [C("Panel", f"Open {n.title()} tab", f"Alt-{i + 1}", (ALT_DIGITS[i],)) for i, n in enumerate(PANEL_TABS)]
        conns = [C("Cell", f"Put cell on conn{i}", f"Ctrl-W {i}", ("c-w", str(i)), "cell") for i in range(CONNECTIONS)]
        return [
            C("Run", "Run cell", "Ctrl-O", ("c-o",), "cell"),
            C("Run", "Run statement under cursor", "F4", ("f4",), "cell"),
            C("Run", "Cancel running / clear input", "Ctrl-C", ("c-c",), "cell"),
            C("Cell", "New cell below", "Ctrl-N", ("c-n",), "cell"),
            C("Cell", "Format SQL", "Ctrl-U", ("c-u",), "cell"),
            C("Cell", "Clear cell text", "L (NORMAL)", ("L",), "cell-normal"),
            C("Cell", "Go to top cell", "$ (NORMAL)", ("$",), "cell-normal"),
            C("Cell", "Go to bottom cell", "% (NORMAL)", ("%",), "cell-normal"),
            C("Cell", "Clear shell (keep input cell)", "Ctrl-L", ("c-l",)),
            *conns,
            C("Focus", "Prompt (input cell)", "Alt-0", (PROMPT_KEY,)),
            C("Focus", "Result pane", "Alt-9", (RESULTS_KEY,)),
            C("Focus", "Next pane", "Ctrl-W w", ("c-w", "w")),
            C("Focus", "Previous pane", "Ctrl-W W", ("c-w", "W")),
            C("Panel", "Show / hide panel", "Ctrl-B", ("c-b",)),
            *tabs,
            C("Schema", "Collect catalog", "Ctrl-O (Schema tab)", ("c-o",), "tab:schema"),
            C("Monitor", "Take snapshot", "Ctrl-O (Monitor tab)", ("c-o",), "tab:monitor"),
            C("Template", "Add template", "a (Template tab)", ("a",), "tab:template"),
            C("Result", "Copy selected value", "y (result pane)", ("y",), "results"),
            C("Result", "Copy selected row", "Y (result pane)", ("Y",), "results"),
            C("Result", "Sort by selected column", "s (result pane)", ("s",), "results"),
            C("Session", "Toggle autocommit", "F7", ("f7",)),
            C("Session", "Toggle autotrace (Oracle)", "F8", ("f8",)),
            C("Session", "Save snapshot and quit", "F1", ("f1",)),
            C("Session", "Quit", "Ctrl-Q", ("c-q",)),
        ]

    def open_palette(self) -> None:
        self._palette_back = self.app.layout.current_window
        self._palette_mode = self.app.vi_state.input_mode
        self.palette.buffer.reset()
        self.palette.index = 0
        self.palette.open = True
        self.app.layout.focus(self.palette.search)
        if self.vi:
            self.app.vi_state.input_mode = InputMode.INSERT

    def _palette_closed(self) -> None:
        back = self._palette_back
        try:
            self.app.layout.focus(back)
        except ValueError:  # gone meanwhile (an emptied cell, the editor closed)
            self._focus_cell(self._session.input_cell, at_end=True)
        if self.vi:
            self.app.vi_state.input_mode = self._palette_mode

    def _run_command(self, c: Command) -> None:
        """Do what the command's shortcut does: put the focus where that key works, then feed
        the key in (once the palette's own key is done)."""
        keys = list(c.keys)
        if c.where in ("cell", "cell-normal"):
            cell = self.cell_of.get(self._palette_back) or self._target_cell()
            if cell.window is not self.app.layout.current_window:
                self.app.layout.focus(cell.window)
            if self.vi and c.where == "cell-normal":
                self.app.vi_state.input_mode = InputMode.NAVIGATION
        elif c.where == "results":
            self.app.layout.focus(self.results.window)
        elif c.where.startswith("tab:"):
            keys = [ALT_DIGITS[PANEL_TABS.index(c.where[4:])]] + keys

        def feed():
            kp = self.app.key_processor
            for k in keys:  # "c-o" / "f4" are Keys, a single character is itself
                kp.feed(KeyPress(k) if isinstance(k, Keys) or len(k) == 1 else KeyPress(Keys(k)))
            kp.process_keys()
        asyncio.get_running_loop().call_soon(feed)

    # -- cells ----------------------------------------------------------
    def _new_session(self, p: Profile) -> Session:
        s = Session(p)
        s.stack = HSplit([])
        s.container = ScrollablePane(s.stack, display_arrows=False)
        s.input_cell = Cell(self, s)
        s.cells.append(s.input_cell)
        s.stack.children.append(s.input_cell.container)
        s.edit_cell = s.input_cell
        s.folder = session_dir(p.name)
        s.file_history = FileHistory(str(s.folder / "history"))
        s.runs = load_history(s.folder / "history")
        self._load_schema(s)
        s.history = history_view(s.runs, MAX_HISTORY_ITEMS)
        snap = take_snapshot(s.folder)
        if snap is not None:
            try:
                self._restore_snapshot(s, snap)
            except Exception:  # a snapshot from an older version: the last run instead
                self._restore_last(s)
        else:
            self._restore_last(s)
        return s

    # -- F1: snapshot the session and quit ---------------------------------
    @staticmethod
    def _grid_state(grid: Grid) -> dict:
        """A result as fetched so far (every page, in the order fetched) and how it is drawn."""
        columns, rows, _cells, kinds, types, _numeric = grid.source
        fetched = grid.original[0] if grid.original is not None else rows
        return {"columns": list(columns), "rows": list(fetched), "kinds": kinds, "types": types,
                "widths": list(grid.widths), "dragged": sorted(grid.dragged), "sort": grid.sort}

    @staticmethod
    def _grid_from(state: dict) -> Grid:
        grid = build(state["columns"], state["rows"], state.get("kinds"), state.get("types"))
        widths = state.get("widths") or []
        if len(widths) == len(grid.widths):
            grid.widths = list(widths)
            grid.dragged = set(state.get("dragged") or [])
            grid._redraw()
        sort = state.get("sort")
        if sort:  # ascending is one step of sort_by, descending two
            for _ in range(2 if sort[1] else 1):
                grid.sort_by(sort[0])
        return grid

    def _snapshot(self, s: Session) -> dict:
        cells = []
        for c in s.cells:  # the input cell last, with its own run (Ctrl-O runs it in place)
            if c.buffer is None:
                cells.append({"note": list(c.output[0]) if c.output else ["class:note", ""]})
                continue
            entry = {"text": c.buffer.text, "cursor": c.buffer.cursor_position, "ran_text": c.ran_text,
                     "output": [list(f) for f in c.output], "title": c.result_title, "trace": c.trace,
                     "more": c.pager is not None, "input": c is s.input_cell, "conn": c.conn_index}
            if c.grid is not None and c.grid.source is not None:
                entry.update(self._grid_state(c.grid))
            cells.append(entry)
        focused = self._focused_cell()
        current = self.app.layout.current_window
        order = s.cells
        index = lambda c: order.index(c) if c in order else None
        return {
            "version": 2, "cells": cells,
            "input": {"text": s.input_cell.buffer.text, "cursor": s.input_cell.buffer.cursor_position},
            "focus": ("results" if current is self.results.window else "panel" if current is self.panel.window
                      else "input" if focused is None else index(focused)),
            "insert": self.app.vi_state.input_mode == InputMode.INSERT,
            "edit": index(s.edit_cell), "shown": index(s.shown), "last_run": index(s.last_run),
            "result": {"row": self.results.row, "col": self.results.col,
                       "scroll": self.results.window.vertical_scroll},
            "panel": {"show": self.show_panel, "tab": self.panel.index,
                      "cursors": [t.cursor for t in self.panel.tabs]},
            "autotrace": s.autotrace,
        }

    def _restore_snapshot(self, s: Session, snap: dict) -> None:
        made: list[Cell] = []
        for entry in snap.get("cells") or []:
            if "note" in entry:
                cell = Cell(self, s, note=tuple(entry["note"]))
            else:
                cell = s.input_cell if entry.get("input") else Cell(self, s)
                text = entry.get("text") or ""
                cell.buffer.set_document(Document(text, min(entry.get("cursor", len(text)), len(text))))
                cell.ran_text = entry.get("ran_text")
                cell.conn_index = entry.get("conn", 0) if 0 <= entry.get("conn", 0) < CONNECTIONS else 0
                cell.output = [tuple(f) for f in entry.get("output") or []]
                cell.result_title = entry.get("title", "")
                cell.trace = entry.get("trace")
                if entry.get("columns"):
                    cell.grid = self._grid_from(entry)
                    cell.data = (entry["columns"], entry["rows"], entry.get("kinds"), entry.get("types"))
                if entry.get("more"):  # the open cursor did not survive: say the rest was not fetched
                    unfetched = lambda t: t.replace(", more ↓)", ", rest not fetched: run again)")
                    cell.result_title = unfetched(cell.result_title)
                    cell.output = [(st, unfetched(t)) for st, t in cell.output]
                cell.version = 1
            if cell is not s.input_cell:
                self._insert_cell(s, cell)
            made.append(cell)
        pick = lambda i: made[i] if isinstance(i, int) and 0 <= i < len(made) else None
        if s.input_cell not in made:  # a version 1 snapshot: the input cell's text apart
            inp = snap.get("input") or {}
            text = inp.get("text") or ""
            s.input_cell.buffer.set_document(Document(text, min(inp.get("cursor", len(text)), len(text))))
        cursors = {c: c.buffer.cursor_position for c in made + [s.input_cell] if c.buffer is not None}
        s.edit_cell = pick(snap.get("edit")) or s.input_cell
        s.shown = pick(snap.get("shown"))
        s.last_run = pick(snap.get("last_run"))
        s.autotrace = bool(snap.get("autotrace", True))
        focus = snap.get("focus")
        s.restore = {"focus": pick(focus) if isinstance(focus, int) else focus,
                     "insert": snap.get("insert", True), "result": snap.get("result") or {},
                     "panel": snap.get("panel") or {}, "saved_at": snap.get("saved_at", ""),
                     "cursors": cursors}

    def _apply_restore(self, s: Session) -> None:
        """After start: focus, cursor and panes as the snapshot left them."""
        r, s.restore = s.restore, None
        if r is None:
            return
        panel = r["panel"]
        if panel:
            self.show_panel = bool(panel.get("show", self.show_panel))
            if 0 <= panel.get("tab", -1) < len(self.panel.tabs):
                self.panel.index = panel["tab"]
            for tab, cur in zip(self.panel.tabs, panel.get("cursors") or []):
                tab.cursor = cur
        focus = r["focus"]
        if isinstance(focus, Cell):
            self.app.layout.focus(focus.window)  # its cursor is where it was
        elif focus == "panel" and self.show_panel:
            self.app.layout.focus(self.panel.window)
        elif focus == "results":
            shown = s.shown
            self.app.layout.focus(self.results.window)
            s.shown = shown  # the pane keeps what it showed, not the latest run
        else:
            self.app.layout.focus(s.input_cell.window)
        for cell, at in r["cursors"].items():  # activate() put the focused one at its end
            cell.buffer.cursor_position = at
        if s.shown is not None:
            self._sync_results()
            if s.shown.grid is not None and self.results.grid is s.shown.grid:
                res = r["result"]
                self.results.row = max(0, min(s.shown.grid.rows - 1, res.get("row", 0)))
                self.results.col = max(0, min(len(s.shown.grid.widths) - 1, res.get("col", 0)))
                self.results.window.vertical_scroll = res.get("scroll", 0)
        if self.vi:
            self.app.vi_state.input_mode = InputMode.INSERT if r["insert"] else InputMode.NAVIGATION
        self.say(f"snapshot from {r['saved_at'].replace('T', ' ')[:16]} restored")

    def snapshot_and_quit(self) -> None:
        s = self._session
        try:
            save_snapshot(s.folder, self._snapshot(s))
        except Exception as e:  # never quit and lose the session silently
            self.say(f"snapshot failed, not quitting: {e}", error=True)
            return
        self.app.exit()

    def _restore_last(self, s: Session) -> None:
        """Bring back the connection's last run: its cell, its result and its trace."""
        last = load_last(s.folder)
        if not last or not last.get("text"):
            return
        cell = Cell(self, s)
        cell.buffer.set_document(Document(last["text"], len(last["text"])))
        cell.ran_text = last["text"]
        cell.conn_index = last.get("conn", 0) if 0 <= last.get("conn", 0) < CONNECTIONS else 0
        cell.output = [tuple(f) for f in last.get("output") or []]
        cell.grid = (build(last["columns"], last["rows"], last.get("kinds"), last.get("types"))
                     if last.get("columns") else None)
        cell.data = (last.get("columns") or [], last["rows"], last.get("kinds"), last.get("types"))
        cell.result_title = last.get("title", "")
        cell.trace = last.get("trace")
        cell.version = 1
        self._insert_cell(s, cell)
        s.last_run = s.shown = cell

    def _save_last(self, s: Session, cell: Cell) -> None:
        columns, rows, kinds, types = (tuple(cell.data) + (None, None))[:4]
        try:
            save_last(s.folder, {"conn": cell.conn_index, "text": cell.ran_text, "output": [list(f) for f in cell.output],
                                 "columns": columns, "rows": rows, "kinds": kinds, "types": types, "title": cell.result_title,
                                 "trace": cell.trace})
        except OSError as e:
            self.say(f"could not save the last run: {e}", error=True)

    def _insert_cell(self, s: Session, cell: Cell) -> None:
        """Add a cell just above the input cell, dropping the oldest beyond MAX_CELLS."""
        s.cells.insert(len(s.cells) - 1, cell)
        s.stack.children.insert(len(s.stack.children) - 1, cell.container)
        while len(s.cells) > MAX_CELLS:
            old = s.cells.pop(0)
            s.stack.children.pop(0)
            self.cell_of.pop(old.window, None)
            if s.edit_cell is old:
                s.edit_cell = s.input_cell
        if hasattr(self, "app"):  # not yet while the last run is restored at start
            self.app.invalidate()

    def set_conn(self, i: int) -> None:
        """Put the focused cell on conn<i>."""
        cell = self._focused_cell()
        if cell is None:
            return
        if cell.running:
            self.say("wait for the cell to finish before moving it to another connection", error=True)
            return
        cell.conn_index = i
        opened = i == 0 or self._session.extra[i - 1] is not None
        self.say(f"cell on conn{i}" + ("" if opened else " (it connects on the first run)"))

    def new_cell_below(self, cell: Cell) -> None:
        """Ctrl-N: an empty cell right below this one (the new bottom, the input cell, when this
        one was), focused in insert mode."""
        s = cell.session
        new = Cell(self, s)
        i = s.cells.index(cell) + 1
        s.cells.insert(i, new)
        s.stack.children.insert(i, new.container)
        if cell is s.input_cell:
            s.input_cell = new
        while len(s.cells) > MAX_CELLS:
            old = s.cells.pop(0)
            s.stack.children.pop(0)
            self.cell_of.pop(old.window, None)
        s.edit_cell = new
        self._focus_cell(new, at_end=True)
        if self.vi:
            self.app.vi_state.input_mode = InputMode.INSERT

    def drop_if_empty(self, cell: Cell) -> None:
        """A cell whose text was all deleted stays, empty, its result gone with the text; one more
        delete key on it (`removable_empty`) removes the cell itself."""
        if not hasattr(self, "app") or get_app().layout.current_window is not cell.window:
            return  # only while the user edits it (not a restore that sets the text)
        if not cell.buffer.text:
            self._clear_result(cell)

    def removable_empty(self) -> bool:
        """The focused cell is empty and not the only one: a delete key on it removes it."""
        cell = self._focused_cell()
        return (cell is not None and cell.buffer is not None and not cell.buffer.text
                and not cell.running and len(self._editable(cell.session)) >= 2)

    def remove_empty(self, cell: Cell) -> None:
        """Remove an empty cell (a delete key on it once it was emptied); the focus moves to the
        next cell, or for the bottom cell to the one above, which becomes the bottom one."""
        s = cell.session
        if cell.buffer.text or cell.running or cell not in s.cells:
            return
        cells = self._editable(s)
        if len(cells) < 2:
            return  # the last cell left stays
        k = cells.index(cell)
        after = cells[k + 1] if k + 1 < len(cells) else cells[k - 1]
        i = s.cells.index(cell)
        s.cells.pop(i)
        s.stack.children.pop(i)
        self.cell_of.pop(cell.window, None)
        self._drop_pager(cell)
        s.pagers = [c for c in s.pagers if c is not cell]
        if cell is s.input_cell:  # the bottom cell went: the one above is the bottom now
            s.input_cell = after
        if s.last_run is cell:
            runs = [c for c in s.cells if c.has_run]
            s.last_run = runs[-1] if runs else None
        if s.shown is cell:
            s.shown = s.last_run
        if s.edit_cell is cell:
            s.edit_cell = after
        self._focus_cell(after, at_end=False)  # at the start: a held backspace stops there
        self.say("empty cell removed")
        self.app.invalidate()

    def _clear_result(self, cell: Cell) -> None:
        """An emptied cell that stays is a fresh one: no `(6 rows)`, no `(stale)`, no result."""
        if not cell.has_run and not cell.output:
            return
        s = cell.session
        self._drop_pager(cell)
        s.pagers = [c for c in s.pagers if c is not cell]
        cell.ran_text, cell.output, cell.grid, cell.trace = None, [], None, None
        cell.data, cell.result_title = ([], [], None), ""
        cell.version += 1
        if s.last_run is cell:
            runs = [c for c in s.cells if c.has_run]
            s.last_run = runs[-1] if runs else None
        if s.shown is cell:
            s.shown = s.last_run
        self.app.invalidate()

    def note(self, s: Session, text: str, style: str = "class:note") -> None:
        """A message line in the shell (connection events and the like)."""
        self._insert_cell(s, Cell(self, s, note=(style, text)))

    def _focused_cell(self) -> Cell | None:
        cell = self.cell_of.get(get_app().layout.current_window)
        return cell if cell is not None and cell.session is self._session else None

    def _editable(self, s: Session) -> list[Cell]:
        return [c for c in s.cells if c.buffer is not None]

    def _focus_cell(self, cell: Cell, at_end: bool) -> None:
        self.app.layout.focus(cell.window)
        cell.buffer.cursor_position = len(cell.buffer.text) if at_end else 0

    def _step(self, cell: Cell, delta: int) -> None:
        """Move focus to the previous/next editable cell (↑ on the first line, ↓ on the last)."""
        cells = self._editable(cell.session)
        i = cells.index(cell) + delta
        if 0 <= i < len(cells):
            self._focus_cell(cells[i], at_end=delta < 0)

    def _sync_results(self, _app=None) -> None:
        """Before each render: show the result of the focused cell in the bottom pane.

        On the input cell that is the latest run; with focus in the panel or the result
        pane itself, the pane keeps what it shows.
        """
        s = self._session
        cell = self._focused_cell()
        if cell is not None:
            s.edit_cell = cell
            s.shown = cell if cell.has_run else s.last_run
        want = s.shown
        key = (id(s), id(want), want.version if want else 0)
        if key == self._shown_key:
            return
        self._shown_key = key
        if want is None:
            self.results.show(None)
        elif want.grid is not None:
            self.results.show(want.grid)
        else:  # DML message or error: show it instead of a table
            text = "".join(t for _, t in want.output).strip()
            error = any(st == "class:error" for st, _ in want.output)
            self.results.show(None, ("class:error" if error else "class:note", text))

    # -- drawing --------------------------------------------------------
    def _history_items(self):
        s, now = self._session, dt.datetime.now()
        digits = len(str(len(s.runs)))  # run numbers, right-aligned: 1 is the oldest run
        return [([("class:list.id", f"{n:>{digits}} "), ("class:list.note", f"{history_stamp(when, now)} ")]
                 + highlighted(" ".join(text.split())), "", "")
                for n, when, text in s.history]

    # -- template: the saved `@alias` templates; add, edit (in vim), rename, delete -----------
    def _template_items(self):
        entries = self.templates.entries()
        head = [([("class:schema.title", "Template"),
                  ("class:list.note", "  Enter/e edit · a add · r rename · d delete · i insert · y copy")],
                 "", "")]
        if not entries:
            return head + [("(none: a adds one)", "class:list.empty", "")]
        idw = len(str(max(t.id for t in entries)))
        aw = max(len(t.alias) for t in entries) + 1
        return head + [([("class:list.id", f"{t.id:>{idw}} "), ("class:schema.title", f"@{t.alias:<{aw}}")]
                        + highlighted(" ".join(t.body.split())), "", "")
                       for t in entries]

    def _template_current(self):
        """The template on the panel's cursor line (the first line is the header)."""
        i = self.panel.tab.cursor - 1
        entries = self.templates.entries()
        return entries[i] if 0 <= i < len(entries) else None

    def _template_edit(self, i: int) -> None:  # Enter (or a click) on a line
        self._template_edit_current()

    def _template_edit_current(self) -> None:
        t = self._template_current()
        if t is None:
            return
        self._tpl_delete = None
        self._open_editor(t.id, t.alias, t.body)

    def _template_add(self) -> None:
        self._tpl_delete = None

        def got(alias: str) -> None:
            self._open_editor(None, alias, "")
        self._ask_for("new alias:", "", lambda a: self.templates.check_alias(a), got)

    def _template_rename(self) -> None:
        t = self._template_current()
        if t is None:
            return
        self._tpl_delete = None

        def got(alias: str, old: str = t.alias) -> None:
            if alias != old:
                self.templates.update(t.id, alias=alias)
                self.say(f"@{old} is @{alias} now")
        self._ask_for(f"rename @{t.alias} to:", t.alias, lambda a: self.templates.check_alias(a, keep=t.id), got)

    def _template_delete(self) -> None:
        t = self._template_current()
        if t is None:
            return
        if self._tpl_delete != t.id:
            self._tpl_delete = t.id
            self.say(f"d again deletes @{t.alias}")
            return
        self._tpl_delete = None
        self.templates.remove(t.id)
        self.say(f"@{t.alias} deleted")

    def _template_insert(self) -> None:
        """`@alias ` at the cursor of the input area: arguments next, then Tab expands it."""
        t = self._template_current()
        if t is None:
            return
        cell = self._target_cell()
        before = cell.buffer.document.text_before_cursor
        cell.buffer.insert_text(("" if not before or before[-1] in " \t\n(,=" else " ") + f"@{t.alias} ")
        self.app.layout.focus(cell.window)
        if self.vi:
            self.app.vi_state.input_mode = InputMode.INSERT

    def _template_copy(self) -> None:
        t = self._template_current()
        if t is not None:
            self.copy_to_clipboard(t.body, f"@{t.alias}")

    def _ask_for(self, label: str, text: str, check: Callable[[str], str | None],
                 done: Callable[[str], None]) -> None:
        """A one-line question under the panel: Enter answers (when `check` finds nothing wrong),
        Esc / Ctrl-C drops it."""
        self._ask = {"label": label, "check": check, "done": done, "back": self.app.layout.current_window}
        self.ask_buffer.set_document(Document(text, len(text)))
        self.app.layout.focus(self.ask_window)
        if self.vi:
            self.app.vi_state.input_mode = InputMode.INSERT

    def _ask_done(self, answer: str | None) -> None:
        ask = self._ask
        if ask is None:
            return
        if answer is not None:
            err = ask["check"](answer)
            if err:
                self.say(err, error=True)
                return
        self._ask = None
        back = ask["back"]
        self.app.layout.focus(back if back is self.editor_window and self._tpl_edit else self.panel.window)
        if self.vi:
            self.app.vi_state.input_mode = InputMode.NAVIGATION
        if answer is None:
            self.say("")
        else:
            ask["done"](answer)

    def _open_editor(self, id: int | None, alias: str, body: str) -> None:
        """Edit a body in the panel (a new template when `id` is None), in INSERT mode."""
        self._tpl_edit = {"id": id, "alias": alias, "saved": body}
        self.editor_buffer.reset(Document(body, 0))
        self.app.layout.focus(self.editor_window)
        if self.vi:
            self.app.vi_state.input_mode = InputMode.INSERT if not body else InputMode.NAVIGATION
        self.say("")

    def _editor_header(self):
        e = self._tpl_edit
        if e is None:
            return []
        changed = self.editor_buffer.text != e["saved"]
        title = f" @{e['alias']}" + (" (new)" if e["id"] is None else f" #{e['id']}") + (" [+]" if changed else "")
        return [("class:tab.active.focused", title + " "),
                ("class:list.note", " :w save · :wq save+close · :q close · :q! drop · $1 $2 … arguments ")]

    def _editor_save(self) -> bool:
        e, body = self._tpl_edit, self.editor_buffer.text.rstrip()
        if not body.strip():
            self.say(f"@{e['alias']}: an empty body is not saved (:q! drops it; d deletes a template)", error=True)
            return False
        try:
            if e["id"] is None:
                t = self.templates.add(e["alias"], body)
                e["id"] = t.id
                self.say(f"@{e['alias']} added")
            else:
                self.templates.update(e["id"], body=body)
                self.say(f"@{e['alias']} saved")
        except TemplateError as err:
            self.say(str(err), error=True)
            return False
        e["saved"] = self.editor_buffer.text
        return True

    def _editor_close(self) -> None:
        e = self._tpl_edit
        self._tpl_edit = None
        ids = [t.id for t in self.templates.entries()]
        if e and e["id"] in ids:
            self.panel.tab.cursor = 1 + ids.index(e["id"])  # the edited line (row 0: the header)
        self.app.layout.focus(self.panel.window)
        if self.vi:
            self.app.vi_state.input_mode = InputMode.NAVIGATION

    def _editor_command(self, cmd: str) -> None:
        """The editor's `:` commands, as in vim: w, wq / x, q, q!."""
        e = self._tpl_edit
        if e is None:
            return
        cmd = cmd.strip()
        changed = self.editor_buffer.text != e["saved"]
        if cmd == "w":
            self._editor_save()
        elif cmd in ("wq", "x"):
            if not (changed or e["id"] is None) or self._editor_save():
                self._editor_close()
        elif cmd == "q":
            if changed:
                self.say("unsaved changes: :wq saves, :q! drops them", error=True)
            else:
                self._editor_close()
        elif cmd == "q!":
            self._editor_close()
            self.say(f"@{e['alias']}: " + ("not added" if e["id"] is None else "changes dropped"))
        elif cmd:
            self.say(f"not an editor command: :{cmd} (w, wq, x, q, q!)", error=True)

    # -- monitor: sessions, blocking, locks, top SQL, waits; refreshed while it shows ---------
    def _monitor_items(self):
        """The last snapshot; a new one only when asked for (Ctrl-O here): dbcli sends nothing
        to the database on its own."""
        s, m = self._session, self._session.watch
        if s.db is None:
            return [("not connected", "class:list.note", "")]
        when = "loading…" if m.loading else m.when.strftime("%H:%M:%S") if m.when else "no snapshot yet"
        head = [([("class:schema.title", "Monitor"), ("class:list.note", f"  {when} · Ctrl-O takes a snapshot")],
                 "", "")]
        return head + [(frags, "", "") for frags in m.lines]

    def _monitor_now(self) -> None:
        s = self._session
        if s.db is None:
            self.say("not connected", error=True)
            return
        if s.watch.loading:
            return
        s.watch.loading = True
        self.app.create_background_task(self._monitor_fetch(s))

    async def _monitor_fetch(self, s: Session) -> None:
        try:
            async with s.lock:
                sections = await asyncio.get_running_loop().run_in_executor(None, monitor.snapshot, s.db)
            s.watch.lines = schema_lines(sections)
            s.watch.when = dt.datetime.now()
        except Exception as e:
            s.watch.lines = [[("class:error", f"  {error_text(e)}")]]
        finally:
            s.watch.loading = False
            self.app.invalidate()

    async def spin_loop(self) -> None:
        """Redraw while a cell runs, so its spinner turns."""
        while True:
            await asyncio.sleep(SPIN_INTERVAL)
            if any(c.running for c in self._session.cells):
                self.app.invalidate()

    # -- schema browser: kinds -> objects -> an object's details (and DDL) --------------
    # -- the schema tab and the catalog outlive the terminal: sessions/<name>/schema.json -----
    def _save_schema(self, s: Session) -> None:
        b = s.browser
        try:
            save_schema(s.folder, {
                "level": b.level, "kinds": b.kinds, "kind": b.kind, "objects": b.objects, "name": b.name,
                "lines": b.lines, "ddl": b.ddl, "cursors": b.cursors,
                "relations": s.relations, "objects_names": sorted(s.objects) if s.objects is not None else None,
                "columns": {t: sorted(c) if c is not None else None for t, c in s.columns.items()},
                "catalog_when": s.catalog_when.isoformat() if s.catalog_when else None,
                "catalog_stale": s.catalog_stale})
        except (OSError, TypeError, ValueError) as e:
            self.say(f"schema tab not saved: {e}", error=True)

    def _load_schema(self, s: Session) -> None:
        d = load_schema(s.folder)
        if not d:
            return
        try:
            b = s.browser
            b.level = d.get("level") if d.get("level") in ("kinds", "objects", "detail") else "kinds"
            b.kinds = [tuple(k) for k in d["kinds"]] if d.get("kinds") is not None else None
            b.kind, b.name, b.ddl = d.get("kind", ""), d.get("name", ""), d.get("ddl", "")
            b.objects = [tuple(o) for o in d.get("objects") or []]
            b.lines = [[tuple(f) for f in line] for line in d.get("lines") or []]
            b.cursors = dict(d.get("cursors") or {})
            s.relations = [tuple(r) for r in d.get("relations") or []]
            if d.get("objects_names") is not None:
                s.objects = set(d["objects_names"])
                s.objects_sorted = sorted(s.objects)
            s.columns = {t: frozenset(c) if c is not None else None for t, c in (d.get("columns") or {}).items()}
            for w in {w for t, cols in s.columns.items() for w in [t, *(cols or ())]} | {n for n, _ in s.relations}:
                self.words.add_base(w, 5)
            s.catalog_when = dt.datetime.fromisoformat(d["catalog_when"]) if d.get("catalog_when") else None
            s.catalog_stale = bool(d.get("catalog_stale"))
        except (KeyError, TypeError, ValueError):
            s.browser = Browser()  # a damaged file: start the tab afresh

    def _schema_items(self):
        s, b = self._session, self._session.browser
        if s.db is None and b.kinds is None:
            return [("not connected", "class:list.note", "")]
        if b.loading:
            return [("loading…", "class:list.note", "")]
        if b.level == "kinds":
            if b.kinds is None:
                return [([("class:schema.title", "Schema"),
                          ("class:list.note", "  Ctrl-O collects the catalog (objects, tables, columns)")], "", ""),
                        ("dbcli reads nothing from the database by itself:", "class:list.note", ""),
                        ("completion and red underlines use what Ctrl-O collected", "class:list.note", "")]
            when = s.catalog_when.strftime("%H:%M:%S") if s.catalog_when else ""
            stale = " · DDL ran since: Ctrl-O again" if s.catalog_stale else ""
            head = [([("class:schema.title", "Schema"), ("class:list.note", f"  {when} · Ctrl-O collects again{stale}")],
                     "", "")]
            return head + [([("class:schema.kind", f"▸ {KIND_LABELS.get(k, k.title())}")], "", str(n) if n else "")
                           for k, n in b.kinds]
        back = ("class:schema.back", "◂ ")
        if b.level == "objects":
            head = [([back, ("class:schema.title", KIND_LABELS.get(b.kind, b.kind.title())),
                      ("class:list.note", f"  {len(b.objects)} · Enter details · i insert name · ⌫ back")], "", "")]
            if not b.objects:
                return head + [("(none)", "class:list.empty", "")]
            return head + [(name, "class:schema.invalid" if note == "INVALID" else "", note) for name, note in b.objects]
        head = [([back, ("class:schema.title", f"{b.kind.lower()} {b.name}"),
                  ("class:list.note", "  d DDL · y copy DDL · i insert name · ⌫ back")], "", "")]
        return head + [(frags, "", "") for frags in b.lines]

    def _schema_pick(self, i: int) -> None:
        b = self._session.browser
        if b.level == "kinds" and b.kinds and 1 <= i <= len(b.kinds):  # row 0: the header
            b.kind, b.cursors["kinds"] = b.kinds[i - 1][0], i
            self._schema_load("objects")
        elif b.level == "objects":
            if i == 0:
                self._schema_back()
            elif b.objects and i - 1 < len(b.objects):
                b.name, b.cursors["objects"] = b.objects[i - 1][0], i
                self._schema_load("detail")
        elif b.level == "detail" and i == 0:
            self._schema_back()

    def _schema_back(self) -> None:
        b = self._session.browser
        if b.level in ("objects", "detail"):
            b.level = "kinds" if b.level == "objects" else "objects"
            self.panel.tab.cursor = b.cursors.get(b.level, 0)
            self._save_schema(self._session)
            self.app.invalidate()

    def _schema_collect(self) -> None:
        """Ctrl-O in the schema tab: read the catalog (object names, tables, columns, the kinds'
        counts) and the level on show again. The only time dbcli reads it."""
        s = self._session
        if s.db is None:
            self.say("not connected", error=True)
            return
        if s.browser.loading:
            return
        s.browser.loading = True
        self.app.create_background_task(self._collect_catalog(s))

    async def _collect_catalog(self, s: Session) -> None:
        b, loop = s.browser, asyncio.get_running_loop()
        try:
            async with s.lock:
                def work():
                    return (s.db.relations(), s.db.object_names(), s.db.table_columns(), schema.kinds(s.db),
                            schema.objects(s.db, b.kind) if b.level == "objects" else None,
                            schema.detail(s.db, b.kind, b.name) if b.level == "detail" else None)
                relations, names, pairs, kinds, objects, detail = await loop.run_in_executor(None, work)
            s.relations, s.objects, s.objects_sorted = relations, names, sorted(names)
            columns: dict[str, set] = {}
            for table, col in pairs:
                columns.setdefault(table.lower(), set()).add(col.lower())
            s.columns = {k: frozenset(v) for k, v in columns.items()}
            for w in {w for pair in pairs for w in pair} | {n for n, _ in relations}:
                self.words.add_base(w, 5)
            b.kinds = kinds
            if objects is not None:
                b.objects = objects
            if detail is not None:
                b.lines, b.ddl = schema_lines(detail), ""
            s.catalog_when, s.catalog_stale = dt.datetime.now(), False
            self._save_schema(s)
            self.say(f"catalog: {len(names)} objects, {len(relations)} tables and views, "
                     f"{len(pairs)} columns")
        except Exception as e:
            self.say(f"catalog: {error_text(e)}", error=True)
        finally:
            b.loading = False
            self.app.invalidate()

    def _schema_insert(self) -> None:
        """The selected object's name (or the one on show) at the cursor of the input area."""
        b = self._session.browser
        name = None
        if b.level == "objects" and 0 < self.panel.tab.cursor <= len(b.objects):
            name = b.objects[self.panel.tab.cursor - 1][0]
        elif b.level == "detail":
            name = b.name
        if not name:
            return
        cell = self._target_cell()
        before = cell.buffer.document.text_before_cursor
        cell.buffer.insert_text(("" if not before or before[-1].isspace() else " ") + name.lower())
        self.app.layout.focus(cell.window)

    def _schema_ddl(self) -> None:
        if self._session.browser.level == "detail":
            self._schema_load("ddl")

    def _schema_copy(self) -> None:
        b = self._session.browser
        if b.level == "detail" and b.ddl:
            self.copy_to_clipboard(b.ddl, f"DDL of {b.name}")
        elif b.level == "detail":
            self.say("no DDL yet: press d first", error=True)

    def _schema_load(self, what: str) -> None:
        s = self._session
        if s.db is None or s.browser.loading:
            return
        s.browser.loading = True
        self.app.create_background_task(self._schema_fetch(s, what))

    async def _schema_fetch(self, s: Session, what: str) -> None:
        b, loop = s.browser, asyncio.get_running_loop()
        try:
            async with s.lock:
                if what == "kinds":
                    b.kinds = await loop.run_in_executor(None, schema.kinds, s.db)
                elif what == "objects":
                    b.objects = await loop.run_in_executor(None, schema.objects, s.db, b.kind)
                    b.level = "objects"
                elif what == "detail":
                    sections = await loop.run_in_executor(None, schema.detail, s.db, b.kind, b.name)
                    b.level, b.ddl = "detail", ""
                    b.lines = schema_lines(sections)
                elif what == "ddl":
                    b.ddl = await loop.run_in_executor(None, schema.ddl, s.db, b.kind, b.name)
                    b.lines = schema_lines_without_ddl(b.lines) + ddl_lines(b.ddl)
            self._save_schema(s)
        except Exception as e:
            self.say(f"schema: {error_text(e)}", error=True)
        finally:
            b.loading = False
            if what in ("objects", "detail"):
                self.panel.tab.cursor = 0 if what == "detail" else 1
            self.app.invalidate()

    def _panel_width(self) -> int:
        if self.panel.tab.name == "monitor":
            return max(PANEL_WIDTH, get_app().output.get_size().columns // 2)
        return self._trace_width()

    def _trace_width(self) -> int:
        """Wide enough for a plan, but never more than half the screen."""
        return max(PANEL_WIDTH, min(TRACE_WIDTH, get_app().output.get_size().columns // 2))

    def _trace_items(self):
        s, shown = self._session, self._session.shown
        if shown is not None and shown.trace:
            return [(frags, "", "") for frags in render_trace(shown.trace)]
        if s.profile.type != "oracle":
            return [("Trace works on Oracle connections only", "class:list.note", "")]
        state = "autotrace on" if s.autotrace else "autotrace off"
        return [(state, "class:list.active" if s.autotrace else "class:list.note", ""), ("", "", ""),
                ("Turn it on with F8 (or click", "class:list.note", ""),
                ("autotrace in the top bar) and", "class:list.note", ""),
                ("run a query: its plans and", "class:list.note", ""),
                ("session statistics show here.", "class:list.note", "")]

    def _cycle_tab(self, buf: Buffer) -> bool:
        """Tab on a word: put the best candidate in its place; Tab again, the next one, and so on
        round (the word as typed comes back after the last). False when there is none."""
        doc = buf.document
        c = self._tab_cycle
        if (c is not None and c["buffer"] is buf and doc.cursor_position == c["end"]
                and doc.text[c["start"]:c["end"]] == c["shown"]
                and not (c["shown"].endswith(" ") and not c.get("table")
                         and in_table_slot(doc.text_before_cursor))):
            # (`fr` + Tab gave `from `: the next Tab goes on to the tables, not to other words)
            c["i"] = (c["i"] + 1) % len(c["items"])
        else:
            m = PARTIAL_RE.search(doc.text_before_cursor)
            prefix = m.group(0) if m else ""
            if doc.text_after_cursor[:1] and WORD_CHAR_RE.match(doc.text_after_cursor[:1]):
                return False  # inside a word
            table = False
            hint = hint_slot(doc.text_before_cursor[:len(doc.text_before_cursor) - len(prefix)])
            if hint is not None:  # in /*+ ... */: every hint, most used first
                items = [match_case(prefix or hint, h) for h in self.words.hint_candidates(prefix)]
                if not items:
                    return False
            elif not prefix:
                # right after FROM / JOIN / INTO / UPDATE or a FROM list's comma: Tab puts a table
                # in, and each further Tab the next one; elsewhere the menu, as before
                before = doc.text_before_cursor
                if (before[-1:].isspace() or before.endswith(",")) and in_table_slot(before):
                    items, table = self.table_candidates(before), True
                else:  # after ON (and = / AND in a join condition): the joined tables' columns;
                    # after WHERE (and AND / OR in it): the statement's tables' columns
                    items = (self.join_column_candidates(before) or self.where_column_candidates(before)
                             or self.select_column_candidates(before, doc.text_after_cursor)
                             or self.order_column_candidates(before)
                             or self.set_column_candidates(before)
                             or self.insert_column_candidates(before))
                if not items:
                    return False
            else:
                items = [w for w in self.words.candidates(prefix, limit=TAB_CANDIDATES, before=doc.text_before_cursor)
                         if w.lower() != prefix.lower()]
            if not items:
                return False
            start = doc.cursor_position - len(prefix)
            space = "" if doc.text_after_cursor[:1].isspace() else " "
            c = self._tab_cycle = {"buffer": buf, "start": start, "end": start + len(prefix),
                                   "shown": prefix, "i": 0, "table": table,
                                   "items": [w + space for w in items] + [prefix]}
        new = c["items"][c["i"]]
        buf.text = doc.text[:c["start"]] + new + doc.text[c["end"]:]
        buf.cursor_position = c["start"] + len(new)
        c["end"], c["shown"] = c["start"] + len(new), new
        n = len(c["items"]) - 1
        self.say(f"Tab {c['i'] + 1}/{n}: {new.strip()}" if c["i"] < n else "Tab: back to what you typed")
        return True

    def join_column_candidates(self, before: str) -> list[str]:
        """Columns for a join condition, as `alias.column`, best first. After `ON` / `AND`: the
        columns the joined table shares with the tables before it (the likely join keys), the
        earlier table's first; then the rest. After `a.x = `: the other tables' `x` first.
        From the catalog Ctrl-O collected in the schema tab; none without it."""
        s = self._session
        text = blank_noise(before)
        on = list(re.finditer(r"\bon\b", text, re.I))
        if not on or re.search(r"\b(where|group|order|having|union|join|select)\b", text[on[-1].end():], re.I):
            return []
        if not re.search(r"(?:\bon|\band|=)\s*$", text, re.I) or not text[-1:].isspace() and not text.endswith("="):
            return []
        tables = [(alias or name, s.columns.get(name.lower()) or frozenset())
                  for _, name, alias in scan(before).tables]
        if len(tables) < 2:
            return []
        out: list[str] = []
        left = re.search(r"([A-Za-z_][\w$#]*)\.([A-Za-z_][\w$#]*)\s*=\s*$", text)
        if left:  # the other side of `a.x = `: x in the other tables first
            lq, lc = left.group(1).lower(), left.group(2).lower()
            others = [(q, cols) for q, cols in tables if q.lower() != lq]
            out += [f"{q}.{lc}" for q, cols in others if lc in cols]
            out += [f"{q}.{c}" for q, cols in others for c in sorted(cols)]
        else:
            (jq, jcols), earlier = tables[-1], tables[:-1]
            shared = sorted({c for _, cols in earlier for c in cols} & jcols)
            out += [f"{q}.{c}" for c in shared for q, cols in earlier if c in cols]
            out += [f"{jq}.{c}" for c in shared]
            out += [f"{jq}.{c}" for c in sorted(jcols)]
            out += [f"{q}.{c}" for q, cols in earlier for c in sorted(cols)]
        seen, items = set(), []
        for c in out:
            if c not in seen:
                seen.add(c)
                items.append(c.lower())
        return items[:TAB_CANDIDATES]

    def where_column_candidates(self, before: str) -> list[str]:
        """Columns for a WHERE (or HAVING) condition, best first by how often and how lately you used them:
        bare `column` for a statement of one table without an alias, else `alias.column`
        (`table.column` for a table without one). From the catalog Ctrl-O collected."""
        text = blank_noise(before)
        where = list(re.finditer(r"\b(?:where|having)\b", text, re.I))
        if not where or re.search(r"\b(group|order|having|union|minus|intersect|select|fetch)\b",
                                  text[where[-1].end():], re.I):
            return []
        if not re.search(r"(?:\bwhere|\bhaving|\band|\bor|\bnot|\()\s*$", text, re.I):
            return []
        return self._column_labels(scan(before).tables)

    def select_column_candidates(self, before: str, after: str) -> list[str]:
        """Columns for a SELECT list (right after SELECT, DISTINCT, a hint or a comma in the
        list), of the tables its FROM names, which may come after the cursor; best first, named
        as for WHERE. From the catalog Ctrl-O collected."""
        text = blank_noise(before)
        sel = list(re.finditer(r"\bselect\b", text, re.I))
        if not sel:
            return []
        head = text[sel[-1].end():]
        if re.search(r"\bfrom\b", head, re.I) or head.count("(") != head.count(")"):
            return []  # past the list, or inside a call's arguments
        if not re.search(r"(?:^|\b(?:distinct|unique|all)|,)\s*$", head, re.I) or not text[-1:].isspace() and not text.endswith(","):
            return []
        rest = after.split(";", 1)[0]
        return self._column_labels(scan(before[sel[-1].start():] + rest).tables)

    def order_column_candidates(self, before: str) -> list[str]:
        """Columns for an ORDER BY or GROUP BY list (right after it, or after a comma in it), of the
        statement's tables, named as for WHERE. From the catalog Ctrl-O collected."""
        text = blank_noise(before)
        order = list(re.finditer(r"\b(?:order|group)\s+by\b", text, re.I))
        if not order:
            return []
        tail = text[order[-1].end():]
        if (re.search(r"\b(select|from|where|having|union|minus|intersect|fetch|offset|limit|for)\b", tail, re.I)
                or tail.count("(") != tail.count(")")):
            return []  # past the list, or inside a call's arguments
        if not re.search(r"(?:^|,)\s*$", tail) or not text[-1:].isspace() and not text.endswith(","):
            return []
        return self._column_labels(scan(before).tables)

    def set_column_candidates(self, before: str) -> list[str]:
        """Columns for an UPDATE's SET list (right after SET, or after a comma in it): the
        updated table's, bare, most used first. In a MERGE's `update set`, the INTO table's.
        From the catalog Ctrl-O collected."""
        s, w = self._session, self.words
        text = blank_noise(before)
        set_ = list(re.finditer(r"\bset\b", text, re.I))
        if not set_:
            return []
        tail = text[set_[-1].end():]
        if re.search(r"\b(where|returning|when|select|from)\b", tail, re.I) or tail.count("(") != tail.count(")"):
            return []
        if not re.search(r"(?:^|,)\s*$", tail) or not text[-1:].isspace() and not text.endswith(","):
            return []
        head = text[:set_[-1].start()]
        target = re.search(r"\bupdate\s+(?:only\s*\()?([A-Za-z_][\w$#.]*)\s*\)?(?:\s+[A-Za-z_][\w$#]*)?\s*$", head, re.I)
        if target is None:  # merge into t using … when matched then update set
            if not re.search(r"\bupdate\s*$", head, re.I):
                return []
            target = list(re.finditer(r"\bmerge\s+into\s+([A-Za-z_][\w$#.]*)", head, re.I))
            target = target[-1] if target else None
        if target is None:
            return []
        name = target.group(1).split(".")[-1].lower()
        cols = s.columns.get(name) or ()
        ordered = sorted(cols, key=lambda c: (-w.ranked(c, lambda k: 0.0), c))
        return ordered[:TAB_CANDIDATES]

    def insert_column_candidates(self, before: str) -> list[str]:
        """Columns for an INSERT's column list (right after `insert into t (`, or after a comma
        in it): the table's, bare, those not listed yet, most used first. In a MERGE's `insert (`,
        the INTO table's. From the catalog Ctrl-O collected."""
        s, w = self._session, self.words
        text = blank_noise(before)
        m = re.search(r"\binto\s+([A-Za-z_][\w$#.]*)(?:\s+[A-Za-z_][\w$#]*)?\s*\(([^()]*)$", text, re.I)
        if m:
            name, listed = m.group(1), m.group(2)
        else:  # merge into t using … when not matched then insert (
            m = re.search(r"\binsert\s*\(([^()]*)$", text, re.I)
            into = list(re.finditer(r"\bmerge\s+into\s+([A-Za-z_][\w$#.]*)", text, re.I))
            if not m or not into:
                return []
            name, listed = into[-1].group(1), m.group(1)
        if not re.search(r"(?:^|,)\s*$", listed):
            return []
        used = {c.lower() for c in WORD_RE.findall(listed)}
        cols = [c for c in s.columns.get(name.split(".")[-1].lower()) or () if c not in used]
        return sorted(cols, key=lambda c: (-w.ranked(c, lambda k: 0.0), c))[:TAB_CANDIDATES]

    def _column_labels(self, tables) -> list[str]:
        """The tables' columns, most used first: bare for one table without an alias, else
        `alias.column` (`table.column` for a table without one)."""
        s, w = self._session, self.words
        if not tables:
            return []
        bare = len(tables) == 1 and not tables[0][2]
        scored = []
        for _, name, alias in tables:
            for c in s.columns.get(name.lower()) or ():
                label = c if bare else f"{(alias or name).lower()}.{c}"
                scored.append((-w.ranked(c, lambda k: 0.0), c, label))
        seen, items = set(), []
        for _, _, label in sorted(scored):
            if label not in seen:
                seen.add(label)
                items.append(label)
        return items[:TAB_CANDIDATES]

    def table_candidates(self, before: str) -> list[str]:
        """Tables for an empty table slot, best first: this schema's own tables and views (from
        the catalog Ctrl-O collected) and any other table you have used, ranked by how often
        and how lately you used them, and how often they followed the word before."""
        s, w = self._session, self.words
        own = {name.lower() for name, _ in s.relations}
        pool = set(own)
        if s.objects is not None:
            pool |= {k for k in s.objects if w._uses.get(k)}
        prev = previous_word(before, "")
        # also the tables you used in any table slot (after FROM, JOIN, INTO, UPDATE or a FROM
        # list's comma): one queried after FROM is offered after JOIN too
        for lead in {"from", "join", "into", "update", prev or "from"}:
            pool |= {k for k in w._pairs.get(lead, {}) if s.objects is None or k in s.objects}
        pool -= set(KEYWORDS)
        bonus = lambda k: 60.0 if k in own else 0.0
        ordered = sorted(pool, key=lambda k: (-w.ranked(k, bonus, prev), len(k), k))
        return [w._display("", k) for k in ordered[:TAB_CANDIDATES]]

    def _keep_pager(self, s: Session, cell: Cell, pager) -> None:
        """Hold a large result's open cursor on its cell; the oldest beyond a few is closed."""
        cell.pager, cell.pager_index = pager, cell.conn_index
        s.pagers = [c for c in s.pagers if c is not cell and c.pager is not None] + [cell]
        while len(s.pagers) > MAX_OPEN_CURSORS:
            old = s.pagers.pop(0)
            self._drop_pager(old)

    @staticmethod
    def _drop_pager(cell: Cell) -> None:
        if cell.pager is not None:
            cell.pager.close()
            cell.pager = None

    def fetch_more(self) -> None:
        """The next page of the result on show, fetched in the background."""
        s = self._session
        cell = s.shown
        if cell is None or cell.pager is None or cell.pager.done or cell.fetching or cell.grid is None:
            return
        cell.fetching = True
        self.app.create_background_task(self._fetch_more(s, cell))

    async def _fetch_more(self, s: Session, cell: Cell) -> None:
        try:
            async with self.lock_for(s, cell.pager_index):
                rows = await asyncio.get_running_loop().run_in_executor(None, cell.pager.fetch, PAGE_ROWS)
            cell.grid.extend(rows)
            done = cell.pager.done
            if done:
                cell.pager = None
            n = cell.grid.rows
            footer = f"({n} row{'s' if n != 1 else ''}{'' if done else ', more ↓'})"
            cell.result_title = RESULT_TITLE_RE.sub(lambda m: m.group(1) + " " + footer, cell.result_title)
            cell.output = [(st, footer if st == "class:note" and t.startswith("(") else t) for st, t in cell.output]
            self.say(f"{n} rows fetched" + ("" if done else " · scroll down for more"))
        except Exception as e:
            self.say(f"fetch more failed: {error_text(e)}", error=True)
            self._drop_pager(cell)
        finally:
            cell.fetching = False
            self.app.invalidate()

    def copy_to_clipboard(self, text: str, what: str = "text") -> None:
        """Copy to the terminal's clipboard: through tmux when inside it (`load-buffer -w` hands
        it on to the outer terminal), else with an OSC 52 escape. Also kept for vi's `p`."""
        self.app.clipboard.set_text(text)
        try:
            if os.environ.get("TMUX"):
                subprocess.run(["tmux", "load-buffer", "-w", "-"], input=text.encode(), timeout=3,
                               check=True, capture_output=True)
            else:
                self.app.output.write_raw("\x1b]52;c;" + base64.b64encode(text.encode()).decode() + "\x07")
                self.app.output.flush()
        except (OSError, subprocess.SubprocessError) as e:
            self.say(f"copy failed: {e}", error=True)
            return
        shown = " ".join(text.split())
        self.say(f"copied {what} ({len(text)} chars): {shown[:40]}{'…' if len(shown) > 40 else ''}")

    def toggle_autotrace(self) -> None:
        s = self._session
        if s.profile.type != "oracle":
            self.say("autotrace works on Oracle connections only", error=True)
            return
        s.autotrace = not s.autotrace  # the top bar's switch shows it: no message as well
        self.app.invalidate()

    def toggle_autocommit(self) -> None:
        s = self._session
        if s.db is None:
            self.say("not connected", error=True)
            return
        if any(c.running for c in s.cells):
            self.say("wait for the running statements to finish", error=True)
            return
        try:  # all of the session's connections alike
            on = not s.db.autocommit
            for db in [s.db] + [d for d in s.extra if d is not None]:
                db.set_autocommit(on)
        except Exception as e:
            self.say(f"autocommit: {str(e).splitlines()[0]}", error=True)
            return
        self.say("autocommit on: changes already made commit with the next statement"
                 if s.db.autocommit else "")

    def _results_title(self):
        s = self._session
        focused = get_app().layout.current_window is self.results.window
        shown = s.shown
        style = "class:title.focused" if focused else "class:title"
        out = [("class:border", "──"), (style, f" Result · {s.profile.name}")]
        if shown is not None and shown.result_title:
            # the statement's keywords as in the input area; the row count after it stays plain
            m = RESULT_TITLE_RE.fullmatch(shown.result_title)
            sql, footer = (m.group(1), m.group(2)) if m else (shown.result_title, "")
            out += [(style, " · ")] + highlighted(sql, base=style) + [(style, footer)]
        tail = ""
        if focused and self.results.grid and self.results.grid.rows:
            tail = f" · row {self.results.row + 1}/{self.results.grid.rows}"
        out.append((style, tail + " "))
        if shown is not None and shown.stale:
            out.append(("class:stale", "(stale) "))
        return out

    def _top_bar(self):
        s = self._session
        state, state_style = STATE_LABEL[s.state]
        if s.running:
            state, state_style = state + " · running…", "class:top.wait"
        busy = sum(1 for c in s.cells if c.running)
        if busy > 1:
            state, state_style = state + f" · {busy} running", "class:top.wait"
        sep = ("class:top.sep", " │ ")
        left = [("class:top", " ")]
        mode = self._vi_mode()
        if mode:
            left += [("class:top.mode", mode.ljust(7))]
        # most important first: a narrow terminal cuts from the right
        left += [(state_style, "●"), ("class:top.name", f" {s.profile.name} "), (state_style, state)]
        hint = self._template_hint()
        if hint or self.message:
            left += [sep, ("class:top.error" if self.error and not hint else "class:top", hint or self.message)]
        # the two switches, each with its key (or click it)
        if s.db is not None:
            on = s.db.autocommit
            left += [sep, ("class:top.mode" if on else "class:top", "autocommit ", clicked(self.toggle_autocommit)),
                     ("class:top.mode" if on else "class:top.off", "on" if on else "off", clicked(self.toggle_autocommit)),
                     ("class:top.dim", " F7")]
        if s.profile.type == "oracle":
            on = s.autotrace
            left += [sep, ("class:top.mode" if on else "class:top", "autotrace ", clicked(self.toggle_autotrace)),
                     ("class:top.mode" if on else "class:top.off", "on" if on else "off", clicked(self.toggle_autotrace)),
                     ("class:top.dim", " F8")]
        left += [sep, ("class:top.dim", f"{s.profile.type} · {s.profile.target()}")]
        right = " Ctrl-P commands · Ctrl-O run · Ctrl-N new cell · Ctrl-0~2 / Ctrl-W 0~3 conn · F4 statement · Ctrl-C cancel · ↑↓ cells · Alt-1~5 panel · Alt-9 result · Alt-0 prompt · Ctrl-W hjkl move · F1 save+quit · Ctrl-Q quit "
        used = sum(get_cwidth(f[1]) for f in left)
        room = get_app().output.get_size().columns - used - get_cwidth(right)
        return left + ([("", " " * room), ("class:top.dim", right)] if room > 0 else [])

    def _vi_mode(self) -> str:
        cell = self._focused_cell()
        if not self.vi or cell is None:
            return ""
        if cell.buffer.selection_state:
            return "VISUAL"
        return {InputMode.INSERT: "INSERT", InputMode.NAVIGATION: "NORMAL",
                InputMode.REPLACE: "REPLACE", InputMode.REPLACE_SINGLE: "REPLACE"}.get(
            get_app().vi_state.input_mode, "NORMAL")

    def _template_hint(self) -> str:
        """While typing `@name …`, show what the template does."""
        cell = self._focused_cell()
        text = cell.buffer.text if cell else ""
        if "\n" in text or not text.lstrip().startswith("@"):
            return ""
        try:
            name, args = self.templates.parse(text)
        except TemplateError:
            return "@name args… calls a template · @ alone, then Ctrl-O: the list"
        return f"@{name} (args {len(args)}/{self.templates.arity(name)}): {self.templates.describe(name)}"

    def say(self, message: str, error: bool = False) -> None:
        self.message, self.error = message, error
        self.app.invalidate()

    # -- key bindings ---------------------------------------------------
    def _global_bindings(self) -> KeyBindings:
        kb = KeyBindings()
        in_shell = Condition(lambda: self._focused_cell() is not None)
        inserting = in_shell & (vi_insert_mode | emacs_insert_mode)

        for keys, direction in [("h", "h"), ("j", "j"), ("k", "k"), ("l", "l"),
                                ("c-h", "h"), ("c-j", "j"), ("c-k", "k"), ("c-l", "l"),
                                ("left", "h"), ("down", "j"), ("up", "k"), ("right", "l")]:
            kb.add("c-w", keys)(self._move_handler(direction))

        @kb.add("c-w", "w")
        @kb.add("c-w", "c-w")
        def _(event):
            event.app.layout.focus_next()

        @kb.add("c-w", "W")
        def _(event):
            event.app.layout.focus_previous()

        for i in range(len(PANEL_TABS)):
            @kb.add(ALT_DIGITS[i])  # Alt-1..5: open that panel tab
            def _(event, i=i):
                self.show_panel = True
                self.panel.select(i)
                editing = self._tpl_edit is not None and self.panel.tab.name == "template"
                event.app.layout.focus(self.editor_window if editing else self.panel.window)

        # PageUp / PageDown always scroll the panel, wherever the focus is
        @kb.add("pageup")
        def _(event):
            if self.show_panel:
                self.panel.page(-1)

        @kb.add("pagedown")
        def _(event):
            if self.show_panel:
                self.panel.page(1)

        # Ctrl+PageUp / Ctrl+PageDown always scroll the result pane
        @kb.add("c-pageup")
        def _(event):
            self.results.page(-1)

        @kb.add("c-pagedown")
        def _(event):
            self.results.page(1)

        @kb.add("f7")
        def _(event):
            self.toggle_autocommit()

        @kb.add("f8")
        def _(event):
            self.toggle_autotrace()

        @kb.add(RESULTS_KEY)
        def _(event):  # Alt+9: the result pane
            event.app.layout.focus(self.results.window)

        @kb.add(PROMPT_KEY)
        def _(event):  # Alt+0: back to the prompt (the input cell) from any pane or earlier cell
            self._focus_cell(self._session.input_cell, at_end=True)
            if self.vi:
                event.app.vi_state.input_mode = InputMode.INSERT

        @kb.add("c-p")
        def _(event):  # the command palette (in the completion menu, Ctrl-P still moves up in it)
            buf = event.app.current_buffer
            if buf.complete_state:
                buf.complete_previous()
                return
            self.open_palette()

        @kb.add("c-b")
        def _(event):
            self.show_panel = not self.show_panel
            if not self.show_panel and event.app.layout.current_window is self.panel.window:
                self._focus_cell(self._session.edit_cell or self._session.input_cell, at_end=True)

        @kb.add("c-l")
        def _(event):  # clear the shell: keep only the input cell
            s = self._session
            for c in s.cells[:-1]:
                self.cell_of.pop(c.window, None)
            s.cells = [s.input_cell]
            s.stack.children[:] = [s.input_cell.container]
            s.last_run = s.shown = None
            s.edit_cell = s.input_cell
            self._focus_cell(s.input_cell, at_end=True)

        @kb.add("tab", filter=inserting)
        def _(event):
            buf = event.current_buffer
            if buf.complete_state:
                buf.complete_next()
                return
            doc = buf.document
            if hint_opening(doc.text_before_cursor, doc.text_after_cursor):
                buf.insert_text(HINT_OPEN)  # `select /` + Tab → `select /*+ `
                return
            recall = HISTORY_REF_RE.search(doc.text_before_cursor)
            if recall:  # `!3` + Tab: run number 3 (1 is the oldest) in its place
                runs = self._session.runs
                n = int(recall.group(1))
                if not 1 <= n <= len(runs):
                    self.say(f"no history entry {n} (1-{len(runs)})", error=True)
                    return
                buf.delete_before_cursor(len(recall.group(0)))
                buf.insert_text(runs[n - 1][1])
                return
            before = doc.text_before_cursor
            if (not before.lstrip().startswith("@") and not self.abbrevs.match(before)
                    and self.templates.call_at(before, doc.text_after_cursor) is None):
                if self._cycle_tab(buf):
                    return
            act = tab_action(doc.text_before_cursor, doc.text_after_cursor, self.abbrevs, self.words,
                             self.templates)
            if act.kind == "error":
                self.say(act.message, error=True)
                return
            if act.kind == "complete":
                buf.start_completion(select_first=False)
                return
            if act.delete:
                buf.delete_before_cursor(act.delete)
            buf.insert_text(act.insert)

        # `(` `,` `)` `.` `;` right after a word Tab put in takes the space Tab added: `to_char(`
        for ch in "(),.;":
            @kb.add(ch, filter=inserting)
            def _(event, ch=ch):
                buf, c = event.current_buffer, self._tab_cycle
                if (c is not None and c["buffer"] is buf and buf.cursor_position == c["end"]
                        and c["shown"].endswith(" ") and buf.document.text_before_cursor.endswith(" ")):
                    buf.delete_before_cursor(1)
                self._tab_cycle = None
                buf.insert_text(ch * event.arg)

        @kb.add("enter", filter=inserting)
        def _(event):  # editor-like: Enter is always a new line
            buf = event.current_buffer
            if buf.complete_state and buf.complete_state.current_completion:
                buf.apply_completion(buf.complete_state.current_completion)
                return
            buf.newline(copy_margin=True)

        # ↑ on the first line / ↓ on the last line move between cells (in every mode);
        # inside a multi-line cell they move the cursor as usual.
        @kb.add("up", filter=in_shell)
        def _(event):
            buf = event.current_buffer
            if buf.complete_state:
                buf.complete_previous()
            elif buf.document.cursor_position_row > 0:
                buf.cursor_up(count=event.arg)
            else:
                self._step(self._focused_cell(), -1)

        @kb.add("down", filter=in_shell)
        def _(event):
            buf = event.current_buffer
            if buf.complete_state:
                buf.complete_next()
            elif buf.document.cursor_position_row < buf.document.line_count - 1:
                buf.cursor_down(count=event.arg)
            else:
                self._step(self._focused_cell(), 1)

        # vim normal mode: k/j do the same at a cell's first/last line
        @kb.add("k", filter=in_shell & vi_navigation_mode)
        def _(event):
            buf = event.current_buffer
            if buf.document.cursor_position_row > 0:
                buf.cursor_up(count=event.arg)
            else:
                self._step(self._focused_cell(), -1)

        @kb.add("j", filter=in_shell & vi_navigation_mode)
        def _(event):
            buf = event.current_buffer
            if buf.document.cursor_position_row < buf.document.line_count - 1:
                buf.cursor_down(count=event.arg)
            else:
                self._step(self._focused_cell(), 1)

        # Ctrl-O runs the focused cell: it reaches the app in every terminal and tmux,
        # and overrides vi's insert-mode Ctrl-O. Ctrl+Enter (when the terminal sends it
        # distinctly), Alt+Enter, F5 and Ctrl-J (some terminals' Ctrl+Enter) do the same.
        @kb.add("c-o", filter=in_shell)
        @kb.add(RUN_KEY, filter=in_shell)
        @kb.add(ALT_ENTER, filter=in_shell)
        @kb.add("f5", filter=in_shell)
        @kb.add("c-j", filter=in_shell)
        def _(event):  # sync on purpose: the text must be taken before more keys arrive
            self.execute_cell(self._focused_cell())

        self._vim_fixes(kb, in_shell)

        # A delete key on a cell already emptied removes the cell (not the only one left):
        # backspace / Delete in INSERT (and emacs) mode, `dd` `x` `X` `D` Shift+L in NORMAL mode.
        removable = Condition(self.removable_empty)
        remove = lambda e: self.remove_empty(self._focused_cell())
        for key in ("backspace", "delete"):
            kb.add(key, filter=inserting & removable)(remove)
        for keys in (("d", "d"), ("x",), ("X",), ("D",), ("L",)):
            kb.add(*keys, filter=in_shell & vi_navigation_mode & removable)(remove)

        # Ctrl-U formats the cell under the cursor (vi's own Ctrl-U, delete to the line's start,
        # gives way; in the panel and the result pane Ctrl-U still scrolls)
        @kb.add("c-u", filter=in_shell)
        def _(event):
            buf = event.current_buffer
            try:
                new = format_sql(buf.text)
            except NotFormatted as e:
                self.say(str(e), error=True)
                return
            if new == buf.text:
                self.say("already formatted")
                return
            buf.save_to_undo_stack()  # `u` (or Ctrl-Z in emacs mode) brings the old text back
            buf.set_document(Document(new, len(new)))
            self.say(f"formatted ({new.count(chr(10)) + 1} lines) · u undoes")

        @kb.add("c-c", filter=~in_shell)
        def _(event):  # in the panel or the result pane: cancel everything running
            self.cancel(self._session.cells)

        @kb.add("c-c", filter=in_shell)
        def _(event):
            cell, s = self._focused_cell(), self._session
            if cell.running:  # a running cell: cancel it
                self.cancel([cell])
                return
            if cell.is_input and not cell.buffer.text and any(c.running for c in s.cells):
                self.cancel(s.cells)  # an empty prompt: cancel whatever runs, like a shell
                return
            if cell.has_run:  # an earlier cell: drop the edits, back to what ran
                cell.buffer.set_document(Document(cell.ran_text, len(cell.ran_text)))
            else:
                cell.buffer.reset()
                event.app.vi_state.input_mode = InputMode.INSERT

        @kb.add("c-d", filter=inserting)
        def _(event):
            if event.current_buffer.text:
                event.current_buffer.delete()
            else:
                event.app.exit()

        @kb.add("c-q")
        def _(event):
            event.app.exit()

        # Ctrl+0..3 put the cell on conn0..conn3. Terminals send Ctrl+2 as NUL (Ctrl+@) and
        # Ctrl+3 as Esc, so those two arrive as such (Ctrl+3 cannot be told from Esc); Ctrl-W
        # then 0..3 does the same in every terminal.
        for i, key in enumerate(CONN_KEYS):
            kb.add(key, filter=in_shell)(lambda e, i=i: self.set_conn(i))
            kb.add("c-w", str(i), filter=in_shell)(lambda e, i=i: self.set_conn(i))
        kb.add("c-@", filter=in_shell)(lambda e: self.set_conn(2))

        @kb.add("$", filter=in_shell & vi_navigation_mode)
        def _(event):  # NORMAL `$` (Shift+4): the top cell; `d$` and visual `$` keep their meaning
            cells = self._editable(self._session)
            if cells:
                self._focus_cell(cells[0], at_end=False)

        @kb.add("%", filter=in_shell & vi_navigation_mode)
        def _(event):  # NORMAL `%` (Shift+5): the bottom cell; `d%` and visual `%` keep their meaning
            self._focus_cell(self._session.input_cell, at_end=True)

        @kb.add("c-n", filter=in_shell)
        def _(event):  # a new cell below the one under the cursor
            buf = event.current_buffer
            if buf.complete_state:  # while the completion menu is open, Ctrl-N still moves in it
                buf.complete_next()
                return
            self.new_cell_below(self._focused_cell())

        @kb.add("f4", filter=in_shell)
        def _(event):  # the complete statement under the cursor, on a connection of its own
            self.run_parallel(self._focused_cell())

        @kb.add("f1")
        def _(event):  # snapshot the session (cells, prompt and cursor, fetched rows) and quit
            self.snapshot_and_quit()

        return kb

    @staticmethod
    def _vim_fixes(kb: KeyBindings, in_shell) -> None:
        """Make prompt_toolkit's vi emulation behave like Vim where it differs."""

        # Typed text: no undo point per character, so `u` undoes the whole insert
        # (the key that entered insert mode already saved the state before it).
        @kb.add(Keys.Any, filter=in_shell & vi_insert_mode, save_before=lambda e: False)
        def _(event):
            event.current_buffer.insert_text(event.data * event.arg)

        # Shift+L in NORMAL mode clears the focused cell (`u` brings the text back). In INSERT
        # mode it stays a capital L.
        @kb.add("L", filter=in_shell & vi_navigation_mode)
        def _(event):
            buf = event.current_buffer
            if buf.text:
                buf.save_to_undo_stack()
                buf.set_document(Document(""), bypass_readonly=True)

        # Ctrl-R is redo in Vim (prompt_toolkit binds it to reverse search instead).
        @kb.add("c-r", filter=in_shell & vi_navigation_mode, save_before=lambda e: False)
        def _(event):
            for _ in range(event.arg):
                event.current_buffer.redo()

        # Vim's `cw` on a word changes to the end of that word (like `ce`), keeping
        # the following space; prompt_toolkit treats it like `dw`.
        for word in (False, True):
            @kb.add("c", "W" if word else "w", filter=in_shell & vi_navigation_mode)
            def _(event, word=word):
                buf = event.current_buffer
                doc = buf.document
                if doc.current_char and not doc.current_char.isspace():
                    n = doc.find_next_word_ending(include_current_position=True,
                                                  count=event.arg, WORD=word)
                else:
                    n = doc.find_next_word_beginning(count=event.arg, WORD=word)
                n = n or len(doc.text_after_cursor)
                event.app.clipboard.set_text(buf.delete(n))
                event.app.vi_state.input_mode = InputMode.INSERT

    def execute_cell(self, cell: Cell) -> None:
        buf, s = cell.buffer, cell.session
        if buf.complete_state:
            buf.cancel_completion()
        text = buf.text
        if not text.strip() or cell.running:
            return
        # it runs in place: Ctrl-N adds a cell for the next statement
        self.app.create_background_task(self.run_cell(s, cell, text))

    def _move_handler(self, direction: str):
        def handler(event):
            screen = event.app.renderer.last_rendered_screen
            current = event.app.layout.current_window
            if screen is None:
                return
            rects = {w: (p.xpos, p.ypos, p.width, p.height)
                     for w, p in screen.visible_windows_to_write_positions.items()
                     if (w in self.fixed_panes or w in self.cell_of) and p.height > 0}
            if current not in rects:
                return
            cur = screen.cursor_positions.get(current)
            target = neighbor(rects, current, direction, (cur.x, cur.y) if cur else None)
            if target is not None:
                event.app.layout.focus(target)
        return handler

    # -- panel actions --------------------------------------------------
    def _target_cell(self) -> Cell:
        s = self._session
        return s.edit_cell if s.edit_cell in s.cells else s.input_cell

    def _recall(self, i: int) -> None:
        text, cell = self._session.history[i][2], self._session.input_cell
        cell.buffer.set_document(Document(text, len(text)))
        self.app.layout.focus(cell.window)

    def _remember(self, s: Session, text: str) -> None:
        s.runs.append((dt.datetime.now(), text))  # the next run number
        s.history = history_view(s.runs, MAX_HISTORY_ITEMS)
        s.file_history.append_string(text)
        self.panel.tabs[PANEL_TABS.index("history")].cursor = 0

    def _completion_context(self):
        """For the word model: the columns of the focused statement's tables, and after `alias.`
        the columns of that alias's table only (both from what is loaded; loading starts here)."""
        cell = self._focused_cell() if hasattr(self, "app") else None
        s = self._session
        if cell is None or s.objects is None:
            return None, None
        doc = cell.buffer.document
        if in_table_slot(doc.text_before_cursor):
            # a table goes here: this schema's own tables and views first, then every other
            # table, view and synonym the user can see (DUAL, V$ views), offered here only
            own = {name.lower() for name, _ in s.relations}
            seen = s.objects
            return (lambda k: 60.0 if k in own else 30.0 if k in seen else 0.0), None, s.objects_sorted
        found = scan(doc.text)
        tables = sorted({t for _, t, _ in found.tables if t.lower() in s.objects}, key=str.lower)
        if not tables:
            return None, None
        known = {t: s.columns.get(t.lower()) for t in tables}
        boost = frozenset().union(*(c for c in known.values() if c))
        m = re.search(r"([A-Za-z_][\w$#]*)\.[\w$#]*$", doc.text_before_cursor)
        if m:
            by_alias = {}
            for _, t, alias in found.tables:
                by_alias[t.lower()] = t
                if alias:
                    by_alias[alias.lower()] = t
            table = by_alias.get(m.group(1).lower())
            if table is not None and known.get(table):
                return boost, known[table]
        return boost, None

    def columns_for(self, s: Session, tables: list[str]) -> dict[str, frozenset | None] | None:
        """The tables' columns as Ctrl-O in the schema tab collected them (None for a table it
        has none of: its columns are not judged); None before any collection."""
        if s.catalog_when is None:
            return None
        return {t: s.columns.get(t.lower()) for t in tables}

    # -- sessions -------------------------------------------------------
    def activate(self, i: int) -> None:
        """Show connection i now; connect in the background if needed."""
        s = self.sessions[self.profiles[i].name]
        if s is not self._session:
            self.active = s.profile.name
            self.panel.tabs[PANEL_TABS.index("schema")].cursor = 0
        self._focus_cell(self._target_cell(), at_end=True)
        if s.db is None:
            if s.connecting is None or s.connecting.done():
                s.connecting = self.app.create_background_task(self.connect(s))

    async def connect(self, s: Session) -> bool:
        s.state = "connecting"
        self.say(f"connecting to {s.profile.name}…")

        try:  # only the connection: the catalog is read when asked for (Ctrl-O in the schema tab)
            s.db = await asyncio.get_running_loop().run_in_executor(None, open_db, s.profile)
        except Exception as e:
            s.state = "failed"
            self.note(s, f"-- connection failed ({s.profile.name}):\n{e}".expandtabs(4), "class:error")
            self.say(f"{s.profile.name}: connection failed", error=True)
            return False
        s.state = "online"
        if s.profile.type == "oracle":
            self.words.add_keywords(HINTS, HINT_WEIGHT)
        self.say(f"{s.profile.name} connected")  # top bar only
        return True

    async def ensure_connected(self, s: Session) -> bool:
        """Connect (or reconnect after the server went offline) before running SQL."""
        if s.db is not None and s.state == "offline":
            old, s.db = s.db, None
            self.note(s, "-- the connection was lost; reconnecting")
            try:
                old.close()
            except Exception:
                pass
        if s.db is None:
            if s.connecting is None or s.connecting.done():
                s.connecting = self.app.create_background_task(self.connect(s))
            return await s.connecting
        return True

    async def ping(self, s: Session) -> None:
        if s.db is None or s.lock.locked():
            return
        async with s.lock:
            try:
                await asyncio.get_running_loop().run_in_executor(None, s.db.ping)
                s.state = "online"
            except Exception:
                s.state = "offline"
        self.app.invalidate()

    async def refresh_suggestions(self) -> None:
        """In the background: recount the suggestions' statistics every minute (taking in what
        other dbcli terminals learned). Local only (words.db): nothing goes to the database."""
        loop, ticks = asyncio.get_running_loop(), 0
        while True:
            await asyncio.sleep(STATS_INTERVAL)
            ticks += 1
            try:
                await loop.run_in_executor(None, self.words.refresh)
            except Exception:
                pass  # words.db busy or gone: the next round tries again
            self.app.invalidate()

    # -- Ctrl-C: cancel what runs --------------------------------------------
    def cancel(self, cells: list[Cell]) -> None:
        """Stop these running cells: the statement on the server is broken off (Oracle answers
        ORA-01013) and the connection stays usable; a cell still waiting never starts."""
        running = [c for c in cells if c.running and not c.cancelled]
        if not running:
            self.say("nothing running to cancel")
            return
        for cell in running:
            cell.cancelled = True
            if cell.conn is not None:
                self.app.create_background_task(self._cancel(cell, cell.conn))
        self.say(f"cancelling {len(running)} statement{'s' if len(running) > 1 else ''}…")

    async def _cancel(self, cell: Cell, conn) -> None:
        try:
            await asyncio.get_running_loop().run_in_executor(None, conn.cancel)
        except Exception as e:
            cell.cancelled = False
            self.say(f"cancel failed: {error_text(e)}", error=True)

    # -- F4: the statement under the cursor, run in parallel ------------------
    def run_parallel(self, source: Cell) -> None:
        """F4: only the complete statement under the cursor, in its own cell (no cell is added),
        on the cell's connection. Cells on different connections run at the same time; those
        on one connection wait their turn, in the order started."""
        s, buf = source.session, source.buffer
        if buf.complete_state:
            buf.cancel_completion()
        if source.running:
            self.say("the cell is still running", error=True)
            return
        stmt = statement_at(buf.text, buf.cursor_position)
        if stmt is None:
            self.say("F4: no complete statement under the cursor (end it with ;)", error=True)
            return
        if not prepare_statement(stmt.text, s.profile.type):
            return
        self.app.create_background_task(self._run_statement(s, source, stmt.text, buf.text))

    async def _run_statement(self, s: Session, cell: Cell, stmt: str, whole: str) -> None:
        await self.run_cell(s, cell, stmt)
        if cell.ran_text == stmt and cell.buffer.text == whole:
            cell.ran_text = whole  # what ran came from this text: the cell is not stale

    # -- the session's connections: conn0 is s.db, conn1..3 open when a cell first runs on them --
    def lock_for(self, s: Session, i: int) -> asyncio.Lock:
        return s.lock if i == 0 else s.extra_locks[i - 1]

    def conn_label(self, s: Session, i: int) -> str:
        return f"{s.profile.name}/conn{i}"

    async def _open_extra(self, s: Session, i: int):
        """conn<i> (1..3), opened on first use with the session's autocommit; None if it fails."""
        db = s.extra[i - 1]
        if db is not None:
            return db
        loop = asyncio.get_running_loop()
        self.say(f"opening conn{i}…")
        db = await loop.run_in_executor(None, open_db, s.profile)
        if s.db is not None:
            try:
                db.set_autocommit(s.db.autocommit)
            except Exception:
                pass  # SQLite: always on
        s.extra[i - 1] = db
        return db

    def _drop_extra(self, s: Session, i: int) -> None:
        """conn<i> (1..3) lost: forget it, so its next run opens it again."""
        db, s.extra[i - 1] = s.extra[i - 1], None
        try:
            db.close()
        except Exception:
            pass

    async def run_cell(self, s: Session, cell: Cell, text: str) -> None:
        """Run a cell's text and store the outcome on the cell (in place, also for re-runs)."""
        cell.running = True
        self._drop_pager(cell)  # a re-run: the earlier result's cursor goes
        self.app.invalidate()
        pre: list[tuple[str, str]] = []
        try:
            if "\n" not in text.strip() and Templates.is_call(text):
                call = text.strip()
                if call == "@":
                    self._remember(s, text)
                    self._finish(s, cell, text, [("class:note", self.templates.listing())])
                    return
                try:
                    sql = self.templates.expand(call)
                except TemplateError as e:
                    self._finish(s, cell, text, [("class:error", f"ERROR: {e}")])
                    return
                pre = [("class:note", "".join(f"-- {l}\n" for l in sql.splitlines()))]
            else:
                sql = prepare_statement(text, s.profile.type)
            n = cell.conn_index
            if n == 0 and not await self.ensure_connected(s):
                self._remember(s, text)  # nothing says it is wrong: keep it
                self._finish(s, cell, text, [("class:error", "not run: not connected")])
                return
            lock = self.lock_for(s, n)
            if lock.locked():
                self.say(f"conn{n}: waiting for its previous statement…")
            cell.waiting = lock.locked()
            async with lock:
                cell.waiting = False
                if cell.cancelled:  # cancelled while it waited for the previous statement
                    self._finish(s, cell, text, [("class:error", "cancelled before it started")])
                    return
                if n:
                    try:
                        db = await self._open_extra(s, n)
                    except Exception as e:
                        self._finish(s, cell, text, [("class:error", f"conn{n}: connection failed: {error_text(e)}")])
                        return
                else:
                    db = s.db
                    s.running = True
                cell.conn = db
                self.say(f"conn{n}: running…")
                traced = s.autotrace and s.profile.type == "oracle"
                trace, lost = None, False
                started = time.monotonic()
                try:
                    if traced:
                        res, trace = await asyncio.get_running_loop().run_in_executor(
                            None, db.execute_traced, sql)
                    else:
                        res = await asyncio.get_running_loop().run_in_executor(None, db.execute, sql)
                    err, bad = None, False
                except Exception as e:  # driver errors are shown, not raised
                    res, err, bad = None, f"ERROR: {error_text(e)}".expandtabs(4), is_parse_error(e)
                    lost = is_connection_error(e)
                    if cell.cancelled:
                        err, bad = f"CANCELLED  ({error_text(e).splitlines()[0]})", True
                finally:
                    if n == 0:
                        s.running = False
                    cell.conn = None
                    stamp = run_stamp(dt.datetime.now(), time.monotonic() - started, cell.cancelled)
            if not bad:  # a syntax or parse error is neither kept in the history nor learned from
                self._remember(s, text)
                self.words.learn(sql)
            self.words.save()  # keep what was learned even if the terminal is killed
            if err:
                self._finish(s, cell, text, pre + [("class:error", err), ("class:note.time", f"\n{stamp}")])
                if lost:  # the connection is gone: the next run on it reconnects
                    if n == 0:
                        s.state = "offline"
                    else:
                        self._drop_extra(s, n)
            elif res.columns:  # a data result: table below, row count in the shell
                footer = f"({res.rowcount} row{'s' if res.rowcount != 1 else ''}" + \
                         (", more ↓)" if res.more else ", first 1000 shown)" if res.truncated else ")")
                if res.more:
                    self._keep_pager(s, cell, res.more)
                first = " ".join(sql.split()).rstrip(";")
                title = f"{first[:60]}{'…' if len(first) > 60 else ''} {footer}"
                self._finish(s, cell, text, pre + [("class:note", footer), ("class:note.time", f"  {stamp}")], build(res.columns, res.rows, res.kinds, res.types), title,
                             trace, (res.columns, res.rows, res.kinds, res.types))
            else:
                self._finish(s, cell, text, pre + [("class:note", format_table(res)), ("class:note.time", f"  {stamp}")],
                             trace=trace)
            if trace is not None:  # like SQL*Plus, the trace shows up by itself
                self.show_panel = True
                self.panel.select(PANEL_TABS.index("trace"))
                self.panel.tabs[PANEL_TABS.index("trace")].cursor = 0
            self.say(f"{self.conn_label(s, n)}: error" if err else f"{self.conn_label(s, n)}: done", error=bool(err))
            if not err and changes_schema(sql) and s.catalog_when is not None:
                s.catalog_stale = True  # read again only when asked: Ctrl-O in the schema tab
        finally:
            cell.running = cell.cancelled = False
            self.app.invalidate()

    def _finish(self, s: Session, cell: Cell, text: str, output, grid: Grid | None = None,
                title: str = "", trace: list[str] | None = None, data=([], [], None)) -> None:
        output = red_cross(output)
        cell.output, cell.grid, cell.result_title, cell.trace = output, grid, title, trace
        cell.data = data
        cell.ran_text = text
        cell.version += 1
        s.last_run = cell
        self._save_last(s, cell)
        if self._focused_cell() is None or self._focused_cell().is_input or self._focused_cell() is cell:
            s.shown = cell  # show it now unless the user moved on to another history cell

    def run(self) -> None:
        try:
            def start():
                self.app.output.write_raw(MODIFY_OTHER_KEYS_ON)  # so Ctrl+Enter is distinct
                self.app.output.flush()
                self.activate(0)
                self._apply_restore(self._session)
                b = self._session.browser  # the schema tab back where it was left
                self.panel.tabs[PANEL_TABS.index("schema")].cursor = 0 if b.level == "detail" else b.cursors.get(b.level, 0)
                self.app.create_background_task(self.refresh_suggestions())
                self.app.create_background_task(self.spin_loop())
            self.app.run(pre_run=start)
        finally:
            sys.stdout.write(MODIFY_OTHER_KEYS_OFF)
            sys.stdout.flush()
            self.words.save()
            for s in self.sessions.values():
                if s.browser.kinds is not None or s.objects is not None:
                    self._save_schema(s)  # the catalog's stale mark too
                for db in [s.db] + s.extra:
                    if db is not None:
                        try:
                            db.close()
                        except Exception:
                            pass


def run(profiles: list[Profile], abbrevs: Abbreviations, ui: UISettings | None = None,
        templates: Templates | None = None) -> None:
    App(profiles, abbrevs, ui or UISettings(), templates or Templates()).run()
