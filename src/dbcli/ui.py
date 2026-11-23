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
import functools
import math
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
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.data_structures import Point
from prompt_toolkit.cursor_shapes import ModalCursorShapeConfig
from prompt_toolkit.document import Document
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import (
    Condition, emacs_insert_mode, has_focus, vi_insert_mode, vi_navigation_mode, vi_selection_mode,
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
from prompt_toolkit.layout.containers import ScrollOffsets
from prompt_toolkit.layout.margins import ConditionalMargin, ScrollbarMargin
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import Processor, TabsProcessor, Transformation
from prompt_toolkit.layout.utils import explode_text_fragments
from prompt_toolkit.mouse_events import MouseButton, MouseEventType
from prompt_toolkit.styles import DynamicStyle
from prompt_toolkit.utils import get_cwidth

from .abbrev import Abbreviations
from .ai import (CELL_REF_RE, Assistant, Chat, Turn, cell_refs, error_message, last_sql, system_prompt,
                 user_message)
from .subscription import activate as subscription_activate, mask as mask_key, save_ui_setting
from .connections import ConnectionError_
from .config import CONFIG_PATH, DATA_DIR, PANEL_TABS, AISettings, Profile, UISettings, load as load_config
from .store import (history_stamp, history_view, load_binds, load_chat, load_history, load_last, load_monitor,
                    load_schema, save_binds, save_chat, save_last, save_monitor, save_schema,
                    save_snapshot,
                    session_dir, take_snapshot)
from .db import (MAX_ROWS, PAGE_ROWS, changes_schema, error_text, format_table, is_connection_error, is_parse_error,
                 open_db, prepare_statement)
from . import monitor, schema
from .formatter import NotFormatted, format_sql
from .statements import Statement, bind_names, script_statements, split_pieces, statement_at
from .themes import DEFAULT_THEME, SWATCH, THEMES, flash_colors, style as theme_style
from .grid import BORDER, HEADER_LINES, Grid, build, copy_text, fit
from .cell_edit import EditError, build_update, source_table
from .objects import NOISE_RE, blank_noise, column_refs, in_table_slot, scan, table_refs
from .oracle_hints import HINT_WEIGHT, HINTS
from .panes import neighbor
from .tab import tab_action
from .templates import KINDS, NAME_PREFIX_RE, TemplateError, Templates
from .trace_view import raw_width as trace_raw_width, render as render_trace
from .words import KEYWORDS, PARTIAL_RE, WORD_CHAR_RE, WORD_RE, WordModel, previous_word

PANEL_WIDTH = 32
TRACE_WIDTH = 96  # the trace tab widens the panel (at most half the screen) so a plan fits
MAX_CELLS = 200  # shell cells kept per session (oldest dropped)
TAB_CANDIDATES = 15  # how many words Tab cycles through
STATS_INTERVAL = 60  # seconds between recounts of the suggestion statistics
# `!N` right before the cursor: Tab puts history entry N there
HISTORY_REF_RE = re.compile(r"(?<![\w$#!])!(\d+)$")
# a result title: the statement (cut at 60) and its row count, "(3 rows)"
# a macro's name being typed at a cell's start: `(`, `(*`, `(*cn` (a macro is a cell's whole text)
MACRO_TYPED_RE = re.compile(r"\s*\(\s*(\*?(?:[^\W\d][\w-]*)?)")
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
# Alt+8 focuses the prompt, Alt+9 the side panel, Alt+0 the result pane (ESC 8 / 9 / 0, one
# key like Alt+1..6).
PROMPT_KEY, PANEL_KEY, RESULTS_KEY = Keys.ControlF17, Keys.ControlF10, Keys.ControlF18
ANSI_SEQUENCES["\x1b8"] = PROMPT_KEY
ANSI_SEQUENCES["\x1b9"] = PANEL_KEY
ANSI_SEQUENCES["\x1b0"] = RESULTS_KEY
# Shift+Tab as tmux sends it once modified keys are asked for (xterm modifyOtherKeys form).
ANSI_SEQUENCES["\x1b[27;2;9~"] = Keys.BackTab
# Alt+Enter and Alt+1/2/3/4 arrive as ESC+key in one burst. Parsing them as single keys
# here (instead of ("escape", key) bindings) keeps a lone Esc instant in vim mode.
ALT_ENTER = Keys.ControlF23
ALT_DIGITS = [Keys.ControlF20, Keys.ControlF21, Keys.ControlF22, Keys.ControlF19, Keys.ControlF12, Keys.ControlF11,
              Keys.ControlF9]
ANSI_SEQUENCES["\x1b\r"] = ALT_ENTER
for _i, _key in enumerate(ALT_DIGITS):
    ANSI_SEQUENCES[f"\x1b{_i + 1}"] = _key
# Ctrl+0..3 put the cell on conn0..conn3. Only sent distinctly with modified keys asked for:
# tmux passes them on in the xterm form, other terminals may use CSI u.
CONN_KEYS = [Keys.ControlF13, Keys.ControlF14, Keys.ControlF15, Keys.ControlF16]
for _i, _key in enumerate(CONN_KEYS):
    ANSI_SEQUENCES[f"\x1b[27;5;{48 + _i}~"] = _key
    ANSI_SEQUENCES[f"\x1b[{48 + _i};5u"] = _key
# Ctrl+F11 / Ctrl+F12 stand in for PageUp / PageDown (keyboards that lack those keys; plain F11
# alone is usually the terminal's fullscreen key). Read the xterm Ctrl-modified function-key
# sequences straight as PageUp/PageDown, so they behave the same everywhere the two do.
ANSI_SEQUENCES["\x1b[23;5~"] = Keys.PageUp
ANSI_SEQUENCES["\x1b[24;5~"] = Keys.PageDown
# Shift+Space, once modified keys are asked for, arrives as ESC [27;2;32~ (or CSI u ESC [32;2u).
# Unknown to prompt_toolkit, it would split into Esc [ 2 7 ... ~ and vim's `~` would flip the
# case under the cursor (`u` undoes). Read it as one key that is just a plain Space.
SHIFT_SPACE = Keys.ControlF6
for _seq in ("\x1b[27;2;32~", "\x1b[32;2u"):
    ANSI_SEQUENCES[_seq] = SHIFT_SPACE
# Alt+← / Alt+→: the shell's previous / next page. Keys of their own (not Esc then an arrow, which
# would hold up a lone Esc in vim): the xterm modifier form and the meta-prefixed one.
# Ctrl+Shift+O: every statement of the cell, in order. Only told from Ctrl-O with modified keys
# asked for: xterm / tmux (Shift as a modifier, or folded into an upper case O) and CSI u forms.
SCRIPT_KEY = Keys.ControlF5
for _seq in ("\x1b[27;6;79~", "\x1b[27;6;111~", "\x1b[27;5;79~", "\x1b[79;6u", "\x1b[111;6u", "\x1b[79;5u"):
    ANSI_SEQUENCES[_seq] = SCRIPT_KEY
# Alt+S (Option+S): the cell split into one cell per statement (vim NORMAL). A key of its own:
# not Esc then `s`. ESC s, and the modified-key forms.
SPLIT_KEY = Keys.ControlF4
for _seq in ("\x1bs", "\x1b[27;3;115~", "\x1b[115;3u"):
    ANSI_SEQUENCES[_seq] = SPLIT_KEY
# Alt+M (Option+M): the Archive tab's Macro page, from anywhere. A key of its own: not Esc then `m`.
MACRO_PAGE_KEY = Keys.ControlF3
for _seq in ("\x1bm", "\x1b[27;3;109~", "\x1b[109;3u"):
    ANSI_SEQUENCES[_seq] = MACRO_PAGE_KEY
PAGE_PREV, PAGE_NEXT = Keys.ControlF7, Keys.ControlF8
for _seq in ("\x1b[1;3D", "\x1b[1;9D", "\x1b\x1b[D"):
    ANSI_SEQUENCES[_seq] = PAGE_PREV
for _seq in ("\x1b[1;3C", "\x1b[1;9C", "\x1b\x1b[C"):
    ANSI_SEQUENCES[_seq] = PAGE_NEXT
MODIFY_OTHER_KEYS_ON, MODIFY_OTHER_KEYS_OFF = "\x1b[>4;1m", "\x1b[>4;0m"

AI_PROMPT = "prompt>"  # the AI tab's question line

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
    marks.update(comment_marks(text, lo, hi))  # last: a comment is gray whatever it holds
    return marks


def comment_marks(text: str, lo: int = 0, hi: int | None = None) -> dict[int, str]:
    """`--` comments, to the end of their line: gray. NOISE_RE walks strings and comments in
    order, so a `--` inside a string ('a--b') is not one."""
    hi = len(text) if hi is None else hi
    marks: dict[int, str] = {}
    for m in NOISE_RE.finditer(text):
        if m.start() >= hi:
            break
        if m.group(0).startswith("--") and m.end() > lo:
            for i in range(max(m.start(), lo), min(m.end(), hi)):
                marks[i] = "class:comment"
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


@functools.lru_cache(maxsize=64)
def text_marks(text: str) -> dict[int, str]:
    """sql_marks of a whole text, kept: the input area asks for it line by line, every redraw,
    and recomputing the whole text for each line made a long cell (a paste) freeze."""
    return sql_marks(text)


@functools.lru_cache(maxsize=64)
def text_runs(text: str) -> tuple:
    """text_marks as runs per line: for each line, ((start col, end col, style), …) where
    consecutive characters share a style. Drawing a line then splits its fragments only where
    a run starts or ends, not into one fragment per character."""
    marks = text_marks(text)
    out, at = [], 0
    for line in text.split("\n"):
        runs: list[list] = []
        for j in range(len(line)):
            st = marks.get(at + j)
            if st is None:
                continue
            if runs and runs[-1][1] == j and runs[-1][2] == st:
                runs[-1][1] = j + 1
            else:
                runs.append([j, j + 1, st])
        out.append(tuple(tuple(r) for r in runs))
        at += len(line) + 1
    return tuple(out)


def restyle(fragments, runs) -> list:
    """The line's fragments with each run's style added over its columns, split only at the
    runs' edges (a fragment keeps its mouse handler)."""
    out, pos, k = [], 0, 0
    for frag in fragments:
        style, text, rest = frag[0], frag[1], frag[2:]
        while text:
            while k < len(runs) and runs[k][1] <= pos:
                k += 1
            if k == len(runs) or runs[k][0] >= pos + len(text):
                out.append((style, text, *rest))  # no run in what is left of this fragment
                pos += len(text)
                break
            a, b, st = runs[k]
            if a > pos:  # plain up to the run
                out.append((style, text[:a - pos], *rest))
                text, pos = text[a - pos:], a
            n = min(b - pos, len(text))
            out.append((f"{style} {st}", text[:n], *rest))
            text, pos = text[n:], pos + n
    return out


def highlighted(text: str, base: str = "class:input") -> list[tuple[str, str]]:
    """`text` as fragments, styled as in the input area (for the history list)."""
    return list(_highlighted(text, base))


@functools.lru_cache(maxsize=4096)
def _highlighted(text: str, base: str) -> tuple:
    # kept by text: the History tab draws every row it has on each redraw
    marks = sql_marks(text)
    out: list[tuple[str, str]] = []
    for i, ch in enumerate(text):
        style = f"{base} {marks[i]}" if i in marks else base
        if out and out[-1][0] == style:
            out[-1] = (style, out[-1][1] + ch)
        else:
            out.append((style, ch))
    return tuple(out)


# a run's text: lit, pulsing slowly (one soft dip a second) as long as it runs and at least
# FLASH_MIN seconds from its start, then fading out over FLASH_FADE
FLASH_MIN, FLASH_FADE, FLASH_PERIOD, FLASH_DIP = 1.0, 0.6, 1.0, 0.45
FADE_INTERVAL = 0.04  # redraws while a mark is lit (25 a second), so it pulses and fades smoothly


def flash_level(started: float, done: float | None, now: float) -> float | None:
    """How lit the text of a run is now, 1 (its full background) to 0; None once it is over
    (the mark can go). It pulses from the start: down by FLASH_DIP and back up every
    FLASH_PERIOD, smoothly (a cosine), for as long as the run goes on (`done` None). It ends
    FLASH_MIN after the start, or FLASH_FADE after the run when that is later, easing out to
    nothing over the last FLASH_FADE."""
    pulse = 1 - FLASH_DIP * 0.5 * (1 - math.cos(2 * math.pi * (now - started) / FLASH_PERIOD))
    if done is None:
        return pulse
    end = max(started + FLASH_MIN, done + FLASH_FADE)
    if now >= end:
        return None
    fade_at = end - FLASH_FADE
    envelope = 1.0 if now < fade_at else 0.5 * (1 + math.cos(math.pi * (now - fade_at) / FLASH_FADE))
    return pulse * envelope


def blend(color: str, base: str, keep: float) -> str:
    """`keep` of color, the rest base (#rrggbb both)."""
    a = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(base[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * keep + y * (1 - keep)):02x}" for x, y in zip(a, b))


class RanHighlight(Processor):
    """The text a cell ran (all of it, the statement of F4, the line of `O`) on a background of
    its own, so the last run is easy to see; after a while it fades into the background around
    it (flash_level). The mark goes when the text is edited."""

    def __init__(self, cell: "Cell"):
        self.cell = cell

    def apply_transformation(self, ti):
        f = self.cell.flash
        level = flash_level(f["started"], f["done"], time.monotonic()) if f is not None else None
        if level is None or ti.document.text != f["text"]:
            return Transformation(ti.fragments)
        lit, terminal, cell_bg = self.cell.app.flash_colors
        base = cell_bg if self.cell.focused() and not self.cell.is_input else terminal
        style = f"bg:{blend(lit, base, level)}"
        line_start = ti.document.translate_row_col_to_index(ti.lineno, 0)
        lo, hi = f["start"] - line_start, f["end"] - line_start
        if hi <= 0 or lo >= len(ti.document.lines[ti.lineno]):
            return Transformation(ti.fragments)
        frags = explode_text_fragments(ti.fragments)
        for i in range(max(0, lo), min(hi, len(frags))):
            frags[i] = (f"{frags[i][0]} {style}", frags[i][1], *frags[i][2:])
        return Transformation(frags)


class ShellPane(ScrollablePane):
    """The shell's scrolling: PageUp / PageDown and the mouse wheel move the view itself, the
    cursor (the focused cell, so the result pane and the Trace tab) staying where it is. Until
    the next key the view stays where it was put; then it follows the cursor again, only as far
    as needed to keep it in view."""

    WHEEL = 3  # lines a turn of the wheel scrolls

    def __init__(self, content, keys: Callable[[], int]):
        self._keys = keys  # how many keys came in so far
        self._free_at = -1  # the key that scrolled the view: it stays put until the next one
        self.last_height = 10
        super().__init__(content, display_arrows=False, keep_focused_window_visible=False,
                         keep_cursor_visible=Condition(lambda: self._keys() != self._free_at),
                         scroll_offsets=ScrollOffsets(top=0, bottom=0))

    reveal_below = 0  # rows under the cursor to bring into view on the next redraw (a paste)

    def _make_window_visible(self, visible_height, virtual_height, visible_win_write_pos, cursor_position):
        # a paste (`p` and the like): its end in view first, as far as the cell goes, then the
        # cursor, which wins when the text is taller than the shell
        if self.reveal_below and cursor_position is not None:
            bottom = visible_win_write_pos.ypos + visible_win_write_pos.height - 1
            end = Point(cursor_position.x, min(cursor_position.y + self.reveal_below, bottom))
            super()._make_window_visible(visible_height, virtual_height, visible_win_write_pos, end)
        self.reveal_below = 0
        super()._make_window_visible(visible_height, virtual_height, visible_win_write_pos, cursor_position)

    def scroll_by(self, lines: int) -> None:
        self._free_at = self._keys()
        self.vertical_scroll = max(0, self.vertical_scroll + lines)  # its end: clipped when drawn
        get_app().invalidate()

    def page(self, direction: int) -> None:
        self.scroll_by(direction * max(1, self.last_height - 1))

    def write_to_screen(self, screen, mouse_handlers, write_position, parent_style, erase_bg, z_index):
        self.last_height = write_position.height
        super().write_to_screen(screen, mouse_handlers, write_position, parent_style, erase_bg, z_index)

    def _copy_over_mouse_handlers(self, mouse_handlers, temp_mouse_handlers, write_position, virtual_width):
        super()._copy_over_mouse_handlers(mouse_handlers, temp_mouse_handlers, write_position, virtual_width)
        # the wheel anywhere over the shell (a cell, its outcome, the blank between) scrolls it
        wrapped: dict = {}

        def wrap(handler):
            if handler not in wrapped:
                def new(event):
                    if event.event_type == MouseEventType.SCROLL_UP:
                        self.scroll_by(-self.WHEEL)
                        return None
                    if event.event_type == MouseEventType.SCROLL_DOWN:
                        self.scroll_by(self.WHEEL)
                        return None
                    return handler(event)
                wrapped[handler] = new
            return wrapped[handler]

        rows = mouse_handlers.mouse_handlers
        for y in range(write_position.ypos, write_position.ypos + write_position.height):
            row = rows[y]
            for x in range(write_position.xpos, write_position.xpos + write_position.width):
                row[x] = wrap(row[x])


class CellControl(BufferControl):
    """A cell's BufferControl whose content is made once per redraw for a given size.
    prompt_toolkit asks a cell for its content several times a redraw (its preferred height,
    the heights' split, the drawing): made anew each time, every line went through the
    processors again, many cells times over. Nothing a content depends on changes while one
    redraw is being made."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._made: tuple | None = None

    def create_content(self, width, height, preview_search=False):
        key = (get_app().render_counter, width, height, preview_search)
        if self._made is not None and self._made[0] == key:
            return self._made[1]
        content = super().create_content(width, height, preview_search)
        self._made = (key, content)
        return content


class SqlHighlight(Processor):
    """The input area's keywords and hint blocks (`/*+ ... */` may span lines), by sql_marks."""

    def apply_transformation(self, ti):
        runs = text_runs(ti.document.text)  # the whole text's, worked out once (not per line)
        line = runs[ti.lineno] if ti.lineno < len(runs) else ()
        if not line:
            return Transformation(ti.fragments)
        return Transformation(restyle(ti.fragments, line))


class MissingObjects(Processor):
    """A red underline under table, view and column names the database does not have."""

    def __init__(self, known: Callable[[], set[str] | None],
                 columns: Callable[[list[str]], dict[str, frozenset | None] | None]):
        self.known = known
        self.columns = columns  # {table: its columns or None}, or None while they are loading
        self._cache: tuple | None = None  # (redraw, text, cursor) -> the text's marked offsets

    def apply_transformation(self, ti):
        names = self.known()
        if not names:  # not connected yet: nothing to check against
            return Transformation(ti.fragments)
        doc = ti.document
        line_start = doc.translate_row_col_to_index(ti.lineno, 0)
        line_end = line_start + len(doc.lines[ti.lineno])
        typing = get_app().layout.current_buffer is ti.buffer_control.buffer
        # the whole text's marks, worked out once per redraw (not once per line, which made a
        # long cell freeze): the names and columns known may change between redraws
        key = (get_app().render_counter, doc.text, doc.cursor_position if typing else None)
        if self._cache is None or self._cache[0] != key:
            self._cache = (key, self._marks(doc, names, typing))
        everything = self._cache[1]
        marks = {i for i in range(line_start, line_end) if i in everything}
        if not marks:
            return Transformation(ti.fragments)
        frags = explode_text_fragments(ti.fragments)
        for i in range(len(frags)):
            if line_start + i in marks:
                frags[i] = (f"{frags[i][0]} class:missing", frags[i][1], *frags[i][2:])
        return Transformation(frags)

    def _marks(self, doc, names: set[str], typing: bool) -> set[int]:
        """Every offset of the text under a missing table, view or column name."""
        marks: set[int] = set()

        def mark(at: int, length: int) -> None:
            if typing and at + length == doc.cursor_position:
                return  # still being typed
            marks.update(range(at, at + length))

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
        return marks


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
        mac = MACRO_TYPED_RE.fullmatch(document.text_before_cursor)
        if mac and self.templates.macro_names(mac.group(1)):  # `(` / `(*c`: the macros, their query as a hint
            prefix = mac.group(1)
            for name in self.templates.macro_names(prefix):
                yield Completion(name, start_position=-len(prefix),
                                 display_meta=" ".join(self.templates._macros[name].split())[:60])
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


def marked_refs(text: str, base: str) -> list[tuple[str, str]]:
    """`text` in `base` style, its `@c0` cell references in bold sky blue."""
    out, at = [], 0
    for m in CELL_REF_RE.finditer(text):
        if m.start() > at:
            out.append((base, text[at:m.start()]))
        out.append(("class:ai.ref", m.group(0)))
        at = m.end()
    if at < len(text) or not out:
        out.append((base, text[at:]))
    return out


class RefHighlight(Processor):
    """The AI question line: `@c0` … in bold sky blue as it is typed (same text, same width)."""

    def __init__(self, active: Callable[[], bool]):
        self.active = active

    def apply_transformation(self, ti):
        if not self.active():
            return Transformation(ti.fragments)
        text = "".join(f[1] for f in ti.fragments)
        return Transformation(marked_refs(text, "class:input"))


def cut_fragments(frags: list[tuple[str, str]], width: int) -> list[tuple[str, str]]:
    """The fragments cut to `width` display cells, ending in … when something was dropped."""
    if sum(get_cwidth(t) for _, t in frags) <= width:
        return frags
    out, left = [], width - 1  # one cell for the …
    for st, t in frags:
        kept = ""
        for ch in t:
            if get_cwidth(ch) > left:
                break
            kept += ch
            left -= get_cwidth(ch)
        if kept:
            out.append((st, kept))
        if kept != t:
            break
    return out + ([("class:list.note", "…")] if width > 0 else [])


def wrap_text(text: str, width: int) -> list[str]:
    """Prose cut to `width` columns at spaces (a long word is cut where it must); blank lines kept."""
    out: list[str] = []
    for para in text.split("\n"):
        line = ""
        for word in para.split(" "):
            while get_cwidth(word) > width:
                if line:
                    out.append(line)
                    line = ""
                out.append(word[:width])
                word = word[width:]
            if line and get_cwidth(line) + 1 + get_cwidth(word) > width:
                out.append(line)
                line = word
            else:
                line = f"{line} {word}" if line else word
        out.append(line)
    return out


def answer_lines(answer: str, width: int) -> list[list[tuple[str, str]]]:
    """An answer as panel lines: prose wrapped, ``` fenced SQL highlighted behind a bar."""
    out: list[list[tuple[str, str]]] = []
    in_code = False
    for raw in answer.split("\n"):
        if raw.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            line = raw.expandtabs(4)
            for k in range(0, max(1, len(line)), max(1, width - 4)):
                out.append([("class:ai.code", "  │ ")] + highlighted(line[k:k + width - 4]))
        else:
            out += [[("class:ai.text", "  " + l)] for l in wrap_text(raw, width - 2)]
    return out


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
        self._cache: tuple | None = None  # (redraw, tab, cursor, focus, width) -> its fragments
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
        for key in {k for t in tabs for k in t.keys}:  # only while a tab that has it shows,
            # so another tab's own key (Ctrl-C, Backspace) keeps its usual meaning
            kb.add(key, filter=Condition(lambda key=key: key in self.tab.keys))(
                lambda e, key=key: self.tab.keys[key]())
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
        # the same list while nothing it shows changed (asked for several times a redraw, and on
        # every redraw): a tab that keeps its rows (History) is then not laid out anew each time
        app = get_app()
        entries = self.tab.items()
        key = (id(entries), len(entries), self.index, self.tab.cursor,
               app.layout.current_window is self.window, self.width(), self._drawn_rows(len(entries)))
        if self._cache is not None and self._cache[0] == key and self._cache[2] is entries:
            return self._cache[1]
        out = self._make_fragments(entries)
        self._cache = (key, out, entries)  # the rows kept too, so their id is not reused
        return out

    def _drawn_rows(self, n: int) -> tuple[int, int] | None:
        """The rows drawn in full: those in view and a screen's worth above and below (and
        around the cursor, which the view may move to). None: all of them (a short list)."""
        info = self.window.render_info
        if info is None or n <= 3 * max(1, info.window_height):
            return None
        h, top, cur = info.window_height, self.window.vertical_scroll, self.tab.cursor
        lo, hi = top - h, top + 2 * h
        if not lo <= cur < hi:  # the view follows the cursor: draw around it as well
            lo, hi = min(lo, cur - h), max(hi, cur + h)
        return max(0, lo), hi

    def _make_fragments(self, entries):
        tab = self.tab
        if not entries:
            return [("class:list.empty", " (none)")]
        tab.cursor = min(tab.cursor, len(entries) - 1)
        focused = get_app().layout.current_window is self.window
        drawn = self._drawn_rows(len(entries))
        out = []
        for i, (label, style, note) in enumerate(entries):
            if drawn is not None and not drawn[0] <= i < drawn[1]:
                # far out of view: a line of its own all the same (the scrollbar and the
                # cursor's place stay right), its colors only when it comes near
                out += [("", " "), ("", "\n")]
                continue
            cur = " class:list.cursor" if focused and i == tab.cursor else ""
            pick = clicked(lambda i=i: self._click(i))
            note = f" {note}" if note else ""
            # a label is plain text, or (style, text) fragments of its own (the trace tab)
            # (a fragment with a click handler of its own keeps it: the Archive tab's pages bar)
            frags = [(style, label)] if isinstance(label, str) else [(f"{style} {f[0]}", *f[1:]) for f in label]
            used = sum(get_cwidth(f[1]) for f in frags)
            pad = max(0, self.width() - 1 - used - get_cwidth(note) - 2)  # 1: scrollbar
            # the selection's background first, so a label's own colors (hints) still show on it
            out += [(style + cur, " ", pick)] + [(f"{cur} {f[0]}", f[1], f[2] if len(f) > 2 else pick) for f in frags]
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
    action: Callable[[], None] | None = None  # called instead, for a command with no key of its own


def palette_match(query: str, c: Command) -> int | None:
    """A rank for `c` (lower is better), None when it does not match: every word of the query
    must appear in the label, the group or the shortcut."""
    hay = f"{c.label} {c.group} {c.shortcut}".lower()
    words = query.lower().split()
    if not all(w in hay for w in words):
        return None
    return 0 if not words or c.label.lower().startswith(words[0]) else 1


PALETTE_WORD_RE = re.compile(r"[\w@$#][\w@$#.-]*")
PALETTE_FIRST = ("panel", "focus", "run")  # Tab offers these first: `p` Tab is `panel` straight away


def palette_completions(query: str, commands: list[Command]) -> tuple[str, list[str]]:
    """Tab in the palette: (the word being typed, the words it can become). The words come from
    the labels and groups of the commands the words before it still match, those that start
    with it (in any case), in the order they first show up; a word typed in full is left out."""
    head, _, part = query.rpartition(" ") if not query.endswith(" ") else (query.rstrip(), " ", "")
    if not part:
        return part, []
    low, seen, out = part.lower(), set(), []
    for c in commands:
        if palette_match(head, c) is None:
            continue
        for w in PALETTE_WORD_RE.findall(f"{c.label} {c.group}"):
            key = w.lower()
            if key.startswith(low) and key != low and key not in seen:
                seen.add(key)
                out.append(w)
    first = [w for name in PALETTE_FIRST for w in out if w.lower() == name]
    return part, first + [w for w in out if w not in first]


def common_prefix(words: list[str]) -> str:
    """The longest start all of them share, ignoring case (in the first one's case)."""
    if not words:
        return ""
    n = len(words[0])
    for w in words[1:]:
        n = min(n, len(w))
        while n and w[:n].lower() != words[0][:n].lower():
            n -= 1
    return words[0][:n]


class CommandPalette:
    """Ctrl-P: every command with its shortcut; type to search, ↑/↓ to choose, Enter to run.
    Tab completes the word being typed from the commands' words (again: the next one)."""

    ROWS = 16

    def __init__(self, commands: list[Command], run: Callable[[Command], None]):
        self.commands, self._run = commands, run
        self.open = False
        self.index = 0
        self.buffer = Buffer(multiline=False, on_text_changed=self._changed)
        self._cycle: dict | None = None  # repeated Tab: {"start", "words", "i"} of the word cycled through
        self._completing = False  # the text changes because Tab put a word in
        kb = KeyBindings()
        kb.add("tab")(lambda e: self.complete(1))
        kb.add("s-tab")(lambda e: self.complete(-1))
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

    def _changed(self, _buf) -> None:
        self.index = 0
        if not self._completing:
            self._cycle = None  # typed on: the next Tab completes afresh

    def complete(self, step: int) -> None:
        """Tab: the word being typed becomes the one word it can be (and a space follows), else
        what all of them start with; a Tab after that goes through them one by one (Shift-Tab
        back)."""
        text = self.buffer.text
        c = self._cycle
        if c is not None:
            c["i"] = (c["i"] + step) % len(c["words"])
            self._set(c["start"] + c["words"][c["i"]])
            return
        part, words = palette_completions(text, self.commands)
        if not words:
            return
        start = text[:len(text) - len(part)]
        if len(words) == 1:
            self._set(start + words[0] + " ")
            return
        prefix = common_prefix(words)
        first = words[0].lower() in PALETTE_FIRST  # panel / focus / run: that word at once, the rest on Tab
        if len(prefix) > len(part) and not first:
            self._set(start + prefix)
            return
        self._cycle = {"start": start, "words": words, "i": 0 if step > 0 else len(words) - 1}
        self._set(start + words[self._cycle["i"]])

    def _set(self, text: str) -> None:
        self._completing = True
        try:
            self.buffer.set_document(Document(text, len(text)))
        finally:
            self._completing = False

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
    """Bottom pane: a result as a box table; j/k rows, h/l columns when focused.

    `i` / `a` / Enter edit the selected cell's value on a line under the table (`I` with the
    cursor at its start, `c` from empty), in vim's INSERT mode; Enter there hands the text to
    `on_edit` (the app runs the UPDATE), Esc in NORMAL mode or Ctrl-C drops it.
    """

    EMPTY = ("class:list.empty", " Query results show up here as a table")

    def __init__(self):
        self.grid: Grid | None = None
        self.on_copy: Callable[[str, str], None] = lambda text, what: None  # set by the app
        self.on_near_end: Callable[[], None] = lambda: None  # set by the app: fetch more rows
        self.on_edit: Callable[[int, int, str], None] = lambda row, col, text: None  # set by the app
        self.on_say: Callable[[str], None] = lambda message: None  # set by the app: an error line
        self.editing: tuple[int, int] | None = None  # (row, column) whose value is being edited
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
        kb.add("enter")(kb.add("i")(kb.add("a")(lambda e: self.start_edit())))
        kb.add("I")(lambda e: self.start_edit(at_end=False))
        kb.add("c")(lambda e: self.start_edit(clear=True))
        self.window = Window(
            ScrollingControl(self._fragments, focusable=True, show_cursor=False, key_bindings=kb,
                             get_cursor_position=self._cursor, move=lambda d: self._move(d, 0),
                             on_mouse=self._mouse),
            wrap_lines=False, height=self._height,
            right_margins=[ScrollbarMargin(display_arrows=True)])

        ekb = KeyBindings()
        ekb.add("enter")(lambda e: self._submit_edit())
        ekb.add("c-c")(lambda e: self.cancel_edit())
        # Esc in INSERT mode goes to NORMAL as in vim; in NORMAL (or emacs mode) it drops the edit
        ekb.add("escape", filter=vi_navigation_mode | emacs_insert_mode)(lambda e: self.cancel_edit())
        self.edit_buffer = Buffer(multiline=False)
        self.edit_window = Window(BufferControl(buffer=self.edit_buffer, key_bindings=ekb),
                                  height=1, get_line_prefix=lambda lineno, wrap: self._edit_prefix(),
                                  style="class:input")
        self.container = HSplit([self.window, ConditionalContainer(
            self.edit_window, filter=Condition(lambda: self.editing is not None))])

    def has_focus(self) -> bool:
        return get_app().layout.current_window in (self.window, self.edit_window)

    def start_edit(self, at_end: bool = True, clear: bool = False) -> None:
        """Edit the selected cell's value on the line under the table (NULL shows as NULL)."""
        if self.grid is None or not self.grid.rows:
            return
        v = self.grid.value(self.row, self.col)
        if isinstance(v, (bytes, bytearray)):
            self.on_say("binary values cannot be edited here")
            return
        text = "" if clear else "NULL" if v is None else copy_text(v)
        self.editing = (self.row, self.col)
        self.edit_buffer.reset(Document(text, len(text) if at_end else 0))  # its own undo, from here
        app = get_app()
        app.layout.focus(self.edit_window)
        if app.editing_mode == EditingMode.VI:
            app.vi_state.input_mode = InputMode.INSERT

    def cancel_edit(self) -> None:
        self.editing = None
        app = get_app()
        if app.layout.current_window is self.edit_window:
            app.layout.focus(self.window)

    def _submit_edit(self) -> None:
        if self.editing is None:
            return
        (row, col), text = self.editing, self.edit_buffer.text
        self.cancel_edit()
        if get_app().editing_mode == EditingMode.VI:
            get_app().vi_state.input_mode = InputMode.NAVIGATION
        self.on_edit(row, col, text)

    def _edit_prefix(self):
        if self.grid is None or self.editing is None:
            return []
        name = self.grid.source[0][self.editing[1]]
        return [("class:prompt.num", f" row {self.editing[0] + 1} "), ("class:prompt", f"{name} = ")]

    @staticmethod
    def _height() -> Dimension:
        """About 35% of the screen, however many rows the result has (a long one scrolls): the
        shell and the panel above get the rest."""
        rows = max(5, get_app().output.get_size().rows * 35 // 100)
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
        if self.editing is not None and grid is not self.grid:
            self.cancel_edit()  # another result: the edit was for the one before
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
        cursor_line = HEADER_LINES + self.row if self.has_focus() and self.grid.rows else -1
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
        self.page: Page = session.page  # the shell page it sits on (made on the page shown)
        self.ran_text: str | None = None  # input as it was last executed
        self.ran_sql: str | None = None  # the statement its result came from (F4 / O run a part of the text)
        self.affected: int | None = None  # rows a DML changed on its last run, None after an error
        self.keep_pane = False  # an UPDATE from the result grid: its outcome does not replace the result shown
        self.flash: dict | None = None  # the text the last run took: {"start", "end", "text", "done"} (RanHighlight)
        self.cursor_hidden = False  # just ran: no cursor over the lit text until a key is pressed
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
                                 on_text_changed=lambda buf: app._cell_edited(self))
            self.window = Window(
                CellControl(buffer=self.buffer, input_processors=[SqlHighlight(),
                                                                MissingObjects(lambda: self.session.objects,
                                                                               lambda t: app.columns_for(self.session, t)),
                                                                RanHighlight(self),
                                                                GhostText(app.words),
                                                                # last: a tab drawn as blanks to the next
                                                                # stop, not `^I` (the others count source columns)
                                                                TabsProcessor(tabstop=4, char1=" ", char2=" ")]),
                dont_extend_height=True, wrap_lines=True, get_line_prefix=self._prefix,
                always_hide_cursor=Condition(lambda: self.cursor_hidden),
                height=self._height,
                right_margins=[ConditionalMargin(ScrollbarMargin(display_arrows=False),
                                                 filter=Condition(self._tall))],
                style=self._style)
            app.cell_of[self.window] = self
            parts.append(self.window)
            # its bind variables' values, right under its text and above its outcome (`running…`):
            # one line each, `0 :cust_id = 20240101`, reached and edited with the vim keys. They
            # show once the cell ran with them (or while a run waits for a value on them).
            self.bind_names: tuple[str, ...] = ()  # the variables of its text, in order
            self.binds_ran = False  # it ran with bind values: its bind lines show
            self.bind_values: dict[str, str] = {}  # its own values, by name (lower case)
            self.bind_buffer = Buffer(multiline=True, on_text_changed=lambda b: app._bind_edited(self))
            self.bind_window = Window(
                BufferControl(buffer=self.bind_buffer, key_bindings=app.bind_kb),
                dont_extend_height=True,
                # the values too; on the cell's focused background with its SQL, as part of it
                style=lambda: ("class:input" if app.binds_active(self) else "class:bind.idle")
                + (" class:cell.focused" if self.focused() and not self.is_input else ""),
                get_line_prefix=lambda n, w: app._bind_prefix(self, n))
            app.bind_of[self.bind_window] = self
            parts.append(ConditionalContainer(self.bind_window, filter=Condition(lambda: app.binds_shown(self))))
        parts.append(ConditionalContainer(
            Window(FormattedTextControl(self._output_fragments), dont_extend_height=True, wrap_lines=True),
            filter=Condition(lambda: bool(self._output_fragments()))))
        parts.append(ConditionalContainer(Window(height=1), filter=Condition(lambda: not self.is_input)))
        self.container = HSplit(parts)

    # A cell taller than this shows this many lines and scrolls inside itself (the cursor kept
    # in view, a scrollbar on the right): the shell draws every line of every cell on each
    # redraw, so a long paste in one cell made every key slow.
    TALL = 30

    def _tall(self) -> bool:
        return self.buffer is not None and self.buffer.document.line_count > self.TALL

    def _height(self):
        return Dimension.exact(self.TALL) if self._tall() else None  # None: as tall as its text

    @property
    def is_input(self) -> bool:
        return self is self.page.input_cell

    @property
    def has_run(self) -> bool:
        return self.ran_text is not None

    @property
    def stale(self) -> bool:
        """Edited since it last ran: its result no longer matches the text."""
        return self.has_run and self.buffer is not None and self.buffer.text.strip() != self.ran_text.strip()

    def focused(self) -> bool:
        """The focus is in the cell: its SQL or its bind lines (they are part of it)."""
        current = get_app().layout.current_window
        return self.window is not None and (current is self.window or current is getattr(self, "bind_window", None))

    def _style(self) -> str:
        return "class:input class:cell.focused" if self.focused() and not self.is_input else "class:input"

    def _prefix(self, lineno: int, wrap_count: int):
        """`#c0 conn0> ` on the first line (the cell's number from the top, a spinner for `>`
        while it runs), `_ ` under it on the last."""
        style = "class:prompt.stale" if self.stale else "class:prompt" if self.focused() else "class:prompt.dim"
        cells = [c for c in self.page.cells if c.buffer is not None]
        tag = f"#c{cells.index(self) if self in cells else 0} "
        label = f"conn{self.conn_index}"
        pad = " " * (len(tag) + len(label))
        if wrap_count:  # a long line carried onto the next row
            return [(style, pad + CONTINUATION)]
        if lineno == 0:
            head = [("class:prompt.num", tag), (f"{style} class:prompt.conn{self.conn_index}", label)]
            if self.running:  # still running: a spinner where ">" goes
                frame = SPINNER[int(time.monotonic() / SPIN_INTERVAL) % len(SPINNER)]
                return head + [("class:prompt.running", frame + " ")]
            return head + [(style, PROMPT)]
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
class Page:
    """One page of the shell: a stack of cells of its own, a tab above the shell. The session's
    `cells`, `input_cell`, `stack` … are those of the page shown."""
    name: str
    cells: list[Cell] = field(default_factory=list)  # oldest first, input cell last
    input_cell: Cell | None = None
    edit_cell: Cell | None = None  # last focused editable cell (panel inserts go here)
    last_run: Cell | None = None  # most recently executed cell
    shown: Cell | None = None  # cell whose result is in the bottom pane
    stack: HSplit | None = None
    container: ScrollablePane | None = None


def _on_page(name: str, doc: str) -> property:
    """A Session attribute that is the current page's."""
    return property(lambda s: getattr(s.page, name), lambda s, v: setattr(s.page, name, v), doc=doc)


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
    binds: dict[str, str] = field(default_factory=dict)  # bind name -> the value a cell set last: a new one starts with it
    browser: "Browser" = field(default_factory=lambda: Browser())  # the schema tab's place
    watch: "Watch" = field(default_factory=lambda: Watch())  # the monitor tab's last snapshot
    chat: Chat = field(default_factory=Chat)  # the AI tab's conversation
    catalog_when: dt.datetime | None = None  # when Ctrl-O in the schema tab last read the catalog
    catalog_stale: bool = False  # a DDL ran since: the catalog may be out of date
    state: str = "idle"  # idle | connecting | online | offline | failed
    running: bool = False
    autotrace: bool = True  # on from the start; F8 / click: plan and statistics in the trace tab (Oracle)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # one DB call at a time
    connecting: asyncio.Task | None = None
    pages: list[Page] = field(default_factory=list)  # the shell's pages, as their tabs show them
    page_index: int = 0  # the page shown
    folder: Path | None = None  # .dbclidata/sessions/<name>
    runs: list[tuple[dt.datetime | None, str]] = field(default_factory=list)  # every run; run N is runs[N-1]
    history: list[tuple[int, dt.datetime | None, str]] = field(default_factory=list)  # the History tab's rows
    file_history: FileHistory | None = None
    extra: list = field(default_factory=lambda: [None] * (CONNECTIONS - 1))  # conn1..conn3, opened on first use
    extra_locks: list = field(default_factory=lambda: [asyncio.Lock() for _ in range(CONNECTIONS - 1)])
    restore: dict | None = None  # where F1's snapshot left the focus, applied once the app runs

    @property
    def page(self) -> Page:
        return self.pages[self.page_index]

    cells = _on_page("cells", "the page's cells, oldest first, input cell last")
    input_cell = _on_page("input_cell", "the page's bottom cell")
    edit_cell = _on_page("edit_cell", "the page's last focused editable cell")
    last_run = _on_page("last_run", "the page's most recently executed cell")
    shown = _on_page("shown", "the page's cell whose result is in the bottom pane")
    stack = _on_page("stack", "the page's cells, as laid out")
    container = _on_page("container", "the page's scrolling shell")


class App:
    def __init__(self, profiles: list[Profile], abbrevs: Abbreviations, ui: UISettings,
                 templates: Templates, ai: AISettings | None = None):
        DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)  # also holds passwords
        self.ai_settings = ai or AISettings()  # as read, subscriptions included
        self._ai_mtime = self._config_mtime()
        self.assistant = Assistant(self.ai_settings.effective())
        self._ai_task = None  # the question being answered (Ctrl-C in the AI tab stops it)
        self._ai_anyway = False  # `a` in the subscription picker: ask with the SDK's own key lookup
        self.profiles = profiles
        self.abbrevs = abbrevs
        self.templates = templates
        self.theme = ui.theme if ui.theme in THEMES else DEFAULT_THEME
        self.style = theme_style(self.theme)  # the app draws with whatever this is (Settings tab: a theme)
        self.flash_colors = flash_colors(self.theme)  # a run's lit text, and what it fades into
        self.words = WordModel(DATA_DIR / "words.db")
        self.words.upper = ui.completion_case == "upper"  # Tab completes SELECT for `sel`
        self.completer = WordCompleter(self.words, self.templates)
        self.words.context = self._completion_context
        self.show_panel = ui.show_panel
        self.trace_raw = False  # `r` in the trace tab: DISPLAY_CURSOR as DBMS_XPLAN's table
        self._history_cache: tuple | None = None  # (what the history is, its rows as drawn)
        self._pane_hold: tuple | None = None  # (cell Ctrl-O went on to, the cell that ran): its result stays
        self.vi = ui.editing_mode == "vi"
        self.close_page_mode = ui.close_page  # delete | confirm
        self.history_max = ui.history_max  # the History tab's statements at most (Settings)
        self.message, self.error = "", False
        self._tab_cycle: dict | None = None  # the word Tab is cycling through, see _cycle_tab
        self._macro_cycle: dict | None = None  # the macro name Tab is cycling through, see _cycle_macro
        self.cell_of: dict[Window, Cell] = {}
        # each cell's bind lines (made with the cell): their window -> the cell, their keys
        self.bind_of: dict[Window, Cell] = {}
        self._bind_wait: dict | None = None  # {"cell", "future"}: a run waits for a value there
        self._keys_in = 0  # keys (and mouse events) come in so far: the shell's free scroll lasts one
        self.bind_kb = self._bind_keys()
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
                              keys={"r": self._trace_toggle_raw}, width=self._trace_width),
            "monitor": PanelTab("monitor", "Monitor", self._monitor_items, lambda i: None,
                                keys={"c-o": self._monitor_now}),
            "archive": PanelTab("archive", "Archive", self._template_items, self._template_pick,
                                keys={"a": self._template_add, "A": self._template_add_cell,
                                      "e": self._template_edit_current, "r": self._template_rename,
                                      "t": self._template_alias, "d": self._template_delete,
                                      "i": self._template_insert, "y": self._template_copy,
                                      "M": self._template_move,
                                      "[": lambda: self._archive_page(-1), "]": lambda: self._archive_page(1)}),
            "settings": PanelTab("settings", "Settings", self._settings_items, self._settings_pick),
            "ai": PanelTab("ai", "AI", self._ai_items, self._ai_pick,
                           keys={"a": self._ai_prompt, "i": self._ai_insert, "y": self._ai_copy,
                                 "c": self._ai_clear, "c-c": self._ai_stop}),
        }
        # every tab as wide as the trace tab needs, so switching tabs never moves the shell;
        # the monitor's wide tables get half the screen
        self.panel = TabbedPanel([tabs[n] for n in PANEL_TABS], ui.panel, width=self._panel_width)
        self._tpl_delete: int | None = None  # the template `d` asked about (a second `d` deletes)
        self.archive_kind = "bookmark"  # the Archive tab's page: bookmark | template | macro ([ / ])
        self._ask: dict | None = None  # the one-line question under the panel (a bookmark's name, its @alias)
        self._bind_lock = asyncio.Lock()  # one run at a time asks for its bind values (F4 runs several)
        self.ask_buffer = Buffer(multiline=False)
        ask_kb = KeyBindings()
        ask_kb.add("enter")(lambda e: self._ask_done(self.ask_buffer.text.strip()))
        ask_kb.add("escape", eager=True)(lambda e: self._ask_done(None))
        ask_kb.add("c-c")(lambda e: self._ask_done(None))
        ask_kb.add("c-u")(lambda e: self.ask_buffer.delete_before_cursor(self.ask_buffer.cursor_position))
        self.ask_window = Window(
            BufferControl(buffer=self.ask_buffer, key_bindings=ask_kb,
                          input_processors=[RefHighlight(lambda: bool(self._ask) and self._ask["label"] == AI_PROMPT)]),
            height=1, style="class:input",
            get_line_prefix=lambda n, w: [("class:ai.prompt" if self._ask and self._ask["label"] == AI_PROMPT
                                           else "class:list.note", f" {self._ask['label']} " if self._ask else " ")])
        # the same question under the shell, for what concerns the shell (a page's name, closing
        # it): the same buffer and keys, only shown there
        self.shell_ask_window = Window(
            BufferControl(buffer=self.ask_buffer, key_bindings=ask_kb), height=1, style="class:input",
            get_line_prefix=lambda n, w: [("class:prompt", f" {self._ask['label']} " if self._ask else " ")])
        asking = Condition(lambda: self._ask is not None and not self._ask["shell"])
        asking_shell = Condition(lambda: self._ask is not None and self._ask["shell"])
        # the Archive editor: the Archive tab's place while a query is edited (vi keys, `:w` …)
        self._tpl_edit: dict | None = None  # {"id": id or None for a new one, "alias", "saved": body}
        self.editor_buffer = Buffer(multiline=True, completer=self.completer, complete_while_typing=False)
        ed_kb = KeyBindings()
        ed_kb.add(":", filter=vi_navigation_mode)(lambda e: self._ask_for(":", "", lambda a: None, self._editor_command))
        ed_kb.add("Z", "Z", filter=vi_navigation_mode)(lambda e: self._editor_command("wq"))
        ed_kb.add("Z", "Q", filter=vi_navigation_mode)(lambda e: self._editor_command("q!"))
        ed_kb.add("c-s")(lambda e: self._editor_command("w"))
        self.editor_window = Window(
            BufferControl(buffer=self.editor_buffer, key_bindings=ed_kb,
                          input_processors=[SqlHighlight(), TabsProcessor(tabstop=4, char1=" ", char2=" ")]),
            wrap_lines=True, style="class:input",
            get_line_prefix=lambda n, w: [("class:list.note", f"{n + 1:>3} " if not w else "    ")])
        editing = Condition(lambda: self._tpl_edit is not None and self.panel.tab.name == "archive")
        editor = HSplit([Window(FormattedTextControl(self._editor_header), height=1, char="─", style="class:border"),
                         self.editor_window], width=lambda: Dimension.exact(self.panel.width()))
        top = VSplit([
            HSplit([Window(FormattedTextControl(self._page_tabs), height=1, style="class:page.bar"),
                    shell, ConditionalContainer(
                HSplit([Window(height=1, char="─", style="class:border"), self.shell_ask_window]),
                filter=asking_shell)]),
            ConditionalContainer(
                VSplit([Window(width=1, char="│", style="class:border"),
                        HSplit([ConditionalContainer(self.panel.container, filter=~editing),
                                ConditionalContainer(editor, filter=editing),
                                ConditionalContainer(self.ask_window, filter=asking)])]),
                filter=Condition(lambda: self.show_panel)),
        ], height=Dimension(weight=3))

        # -- results (bottom, full width, ~35% of the height) --
        self.results = ResultGrid()
        self.results.on_copy = self.copy_to_clipboard
        self.results.on_near_end = self.fetch_more
        self.results.on_edit = self.edit_value
        self.results.on_say = lambda message: self.say(message, error=True)
        # a heavy rule, apart from the thin ones elsewhere: the shell above, the result below
        results_title = Window(FormattedTextControl(self._results_title), height=1, char="━",
                               style="class:result.rule")

        # -- top bar: connection and online state --
        top_bar = HSplit([Window(FormattedTextControl(self._top_bar), height=1),
                          Window(height=1, char="─", style="class:border")])

        self.fixed_panes = [self.panel.window, self.results.window]
        self.palette = CommandPalette(self._commands(), self._run_command)
        self.palette.on_close = self._palette_closed
        self._palette_back = None  # the window focused when the palette opened
        root = FloatContainer(HSplit([top_bar, top, results_title, self.results.container]), floats=[
            Float(top=2, content=self.palette.container),
            Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=10)),
        ])
        first = self._session.input_cell.window
        # true color where the terminal says it has it (COLORTERM), so the soft shades show as
        # written; otherwise prompt_toolkit's 256 colors, which round them to the nearest entry
        depth = ColorDepth.DEPTH_24_BIT if os.environ.get("COLORTERM") in ("truecolor", "24bit") else None
        self.app = Application(layout=Layout(root, focused_element=first), color_depth=depth,
                               key_bindings=self._global_bindings(), style=DynamicStyle(lambda: self.style),
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
            C("Run", "Run all statements in order (script)", "Ctrl+Shift+O", (SCRIPT_KEY,), "cell"),
            C("Run", "Run line under cursor", "O (NORMAL)", ("O",), "cell-normal"),
            C("Run", "Cancel running / clear input", "Ctrl-C", ("c-c",), "cell"),
            C("Cell", "New cell below", "Ctrl-N", ("c-n",), "cell"),
            C("Cell", "Delete the cell", "Ctrl-D", ("c-d",), "cell"),
            C("Cell", "Split cell into one cell per statement", "Alt-S (NORMAL)", (SPLIT_KEY,), "cell-normal"),
            C("Cell", "Format SQL", "Ctrl-U", ("c-u",), "cell"),
            C("Cell", "Clear cell text", "L (NORMAL)", ("L",), "cell-normal"),
            C("Cell", "Go to top cell", "gg (NORMAL)", ("g", "g"), "cell-normal"),
            C("Cell", "Go to bottom cell", "G (NORMAL)", ("G",), "cell-normal"),
            C("Cell", "Clear shell (keep input cell)", "Ctrl-L", ("c-l",)),
            C("Page", "New page", "Ctrl-T", ("c-t",)),
            C("Page", "Next page", "Alt-→ / gt", (PAGE_NEXT,)),
            C("Page", "Previous page", "Alt-← / gT", (PAGE_PREV,)),
            C("Page", "Close page", "", (), action=self.close_page),
            C("Page", "Rename page", "", (), action=self.rename_page),
            C("Page", "Clear page (every cell goes, one empty cell stays)", "", (), action=self.clear_page),
            *[C("Page", f"Focus page {i + 1}: {p.name}", f"{i + 1}gt", (), action=lambda i=i: self.show_page(i))
              for i, p in enumerate(self._session.pages)],
            *conns,
            C("Focus", "Prompt (input cell)", "Alt-8", (PROMPT_KEY,)),
            C("Focus", "Side panel", "Alt-9", (PANEL_KEY,)),
            C("Focus", "Result pane", "Alt-0", (RESULTS_KEY,)),
            C("Focus", "Next pane", "Ctrl-W w", ("c-w", "w")),
            C("Focus", "Previous pane", "Ctrl-W W", ("c-w", "W")),
            C("Panel", "Show / hide panel", "Ctrl-B", ("c-b",)),
            *tabs,
            C("Schema", "Collect catalog", "Ctrl-O (Schema tab)", ("c-o",), "tab:schema"),
            C("Monitor", "Take snapshot", "Ctrl-O (Monitor tab)", ("c-o",), "tab:monitor"),
            C("Archive", "Add to the Archive page shown", "a (Archive tab)", ("a",), "tab:archive"),
            C("Archive", "Keep the current cell's SQL in the Archive", "Ctrl-R", ("c-r",), "cell"),
            C("Cell", "Duplicate the cell below", "P (NORMAL)", ("P",), "cell-normal"),
            C("Archive", "Go to Macro page", "Alt-M", (MACRO_PAGE_KEY,)),
            C("Archive", "Next Archive page (Bookmark, Template, Macro)", "] (Archive tab)", ("]",), "tab:archive"),
            C("Archive", "Previous Archive page", "[ (Archive tab)", ("[",), "tab:archive"),
            C("Archive", "Rename a template or macro", "t (Archive tab)", ("t",), "tab:archive"),
            C("Archive", "Move an entry to another Archive page", "M (Archive tab)", ("M",), "tab:archive"),
            C("AI", "Ask AI a question", "a (AI tab)", ("a",), "tab:ai"),
            C("AI", "Insert SQL from the last answer", "i (AI tab)", ("i",), "tab:ai"),
            C("AI", "New AI chat", "c (AI tab)", ("c",), "tab:ai"),
            C("Result", "Copy selected value", "y (result pane)", ("y",), "results"),
            C("Result", "Copy selected row", "Y (result pane)", ("Y",), "results"),
            C("Result", "Sort by selected column", "s (result pane)", ("s",), "results"),
            C("Session", "Toggle autocommit", "F7", ("f7",)),
            C("Session", "Toggle autotrace (Oracle)", "F8", ("f8",)),
            C("Session", "Save snapshot and quit", "F1", ("f1",)),
            C("Session", "Quit", "Ctrl-Q", ("c-q",)),
        ]

    def open_palette(self) -> None:
        self.palette.commands = self._commands()  # made anew: the pages there are now
        self._palette_back = self.app.layout.current_window
        self._palette_mode = self.app.vi_state.input_mode
        self.palette.buffer.reset()
        self.palette.index = 0
        self.palette._cycle = None
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
        if c.action is not None:
            asyncio.get_running_loop().call_soon(c.action)
            return
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
    def _new_page(self, s: Session, name: str, at: int | None = None) -> Page:
        """A page with one empty cell, put at `at` (else last) and shown."""
        page = Page(name)
        page.stack = HSplit([])
        # the shell scrolls only when it must: PageUp / PageDown, or the cursor going past its top or
        # bottom edge (or nothing left to show below). Not to show a cell whole when the focus moves
        # onto it, nor a line before the edge.
        page.container = ShellPane(page.stack, lambda: self._keys_in)
        at = len(s.pages) if at is None else at
        s.pages.insert(at, page)
        s.page_index = at  # shown, so the cell is made on it
        page.input_cell = page.edit_cell = Cell(self, s)
        page.cells.append(page.input_cell)
        page.stack.children.append(page.input_cell.container)
        return page

    def _new_session(self, p: Profile) -> Session:
        s = Session(p)
        self._new_page(s, "main")
        s.folder = session_dir(p.name)
        s.binds = load_binds(s.folder)  # the bind values cells set last: new cells start with them
        s.file_history = FileHistory(str(s.folder / "history"))
        s.runs = load_history(s.folder / "history")
        self._load_schema(s)
        self._load_monitor(s)
        s.chat = Chat.from_json(load_chat(s.folder))  # the AI tab's latest questions, from the last run
        s.history = history_view(s.runs, self.history_max)
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

    def _page_snapshot(self, page: Page) -> dict:
        cells = []
        for c in page.cells:  # the input cell last, with its own run (Ctrl-O runs it in place)
            if c.buffer is None:
                cells.append({"note": list(c.output[0]) if c.output else ["class:note", ""]})
                continue
            entry = {"text": c.buffer.text, "cursor": c.buffer.cursor_position, "ran_text": c.ran_text,
                     "output": [list(f) for f in c.output], "title": c.result_title, "trace": c.trace,
                     "more": c.pager is not None, "input": c is page.input_cell, "conn": c.conn_index,
                     "binds": dict(c.bind_values), "binds_ran": c.binds_ran}
            if c.grid is not None and c.grid.source is not None:
                entry.update(self._grid_state(c.grid))
            cells.append(entry)
        index = lambda c: page.cells.index(c) if c in page.cells else None
        return {"name": page.name, "cells": cells, "edit": index(page.edit_cell), "shown": index(page.shown),
                "last_run": index(page.last_run)}

    def _snapshot(self, s: Session) -> dict:
        focused = self._focused_cell()
        current = self.app.layout.current_window
        return {
            "version": 3, "pages": [self._page_snapshot(p) for p in s.pages], "page": s.page_index,
            # where the focus is: in the page shown
            "focus": ("results" if current is self.results.window else "panel" if current is self.panel.window
                      else "input" if focused is None else s.cells.index(focused)),
            "insert": self.app.vi_state.input_mode == InputMode.INSERT,
            "result": {"row": self.results.row, "col": self.results.col,
                       "scroll": self.results.window.vertical_scroll},
            "panel": {"show": self.show_panel, "tab": self.panel.index,
                      "cursors": [t.cursor for t in self.panel.tabs]},
            "autotrace": s.autotrace,
        }

    def _restore_snapshot(self, s: Session, snap: dict) -> None:
        # version 3: pages; before, the cells of one page (version 1: the input cell's text apart)
        pages = snap.get("pages") or [{"name": "main", **{k: snap.get(k) for k in ("cells", "input", "edit",
                                                                                    "shown", "last_run")}}]
        made_of: list[list[Cell]] = []
        for i, entry in enumerate(pages):
            if i:
                self._new_page(s, str(entry.get("name") or f"page{i + 1}"))
            else:
                s.page.name = str(entry.get("name") or "main")
            made_of.append(self._restore_page(s, entry))
        s.page_index = snap["page"] if isinstance(snap.get("page"), int) and 0 <= snap["page"] < len(s.pages) else 0
        made = made_of[s.page_index]
        pick = lambda i: made[i] if isinstance(i, int) and 0 <= i < len(made) else None
        cursors = {c: c.buffer.cursor_position for cells in made_of for c in cells if c.buffer is not None}
        cursors.update({p.input_cell: p.input_cell.buffer.cursor_position for p in s.pages})
        s.autotrace = bool(snap.get("autotrace", True))
        focus = snap.get("focus")
        s.restore = {"focus": pick(focus) if isinstance(focus, int) else focus,
                     "insert": snap.get("insert", True), "result": snap.get("result") or {},
                     "panel": snap.get("panel") or {}, "saved_at": snap.get("saved_at", ""),
                     "cursors": cursors}

    def _restore_page(self, s: Session, snap: dict) -> list[Cell]:
        """The page shown gets the snapshot's cells for it; the cells made, in order."""
        made: list[Cell] = []
        for entry in snap.get("cells") or []:
            if "note" in entry:
                cell = Cell(self, s, note=tuple(entry["note"]))
            else:
                cell = s.input_cell if entry.get("input") else Cell(self, s)
                cell.bind_values = {str(k): str(v) for k, v in (entry.get("binds") or {}).items()}
                cell.binds_ran = bool(entry.get("binds_ran"))
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
        s.edit_cell = pick(snap.get("edit")) or s.input_cell
        s.shown = pick(snap.get("shown"))
        s.last_run = pick(snap.get("last_run"))
        return made

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
        cell.bind_values = {str(k): str(v) for k, v in (last.get("binds") or {}).items()}  # as it last ran
        cell.binds_ran = bool(last.get("binds_ran"))
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
                                 "trace": cell.trace, "binds": dict(cell.bind_values), "binds_ran": cell.binds_ran})
            if cell.bind_values:
                save_binds(s.folder, s.binds)  # the values set last, for a new cell after a restart
        except OSError as e:
            self.say(f"could not save the last run: {e}", error=True)

    def _insert_cell(self, s: Session, cell: Cell) -> None:
        """Add a cell just above the input cell of its page, dropping the oldest beyond MAX_CELLS."""
        p = cell.page
        p.cells.insert(len(p.cells) - 1, cell)
        p.stack.children.insert(len(p.stack.children) - 1, cell.container)
        while len(p.cells) > MAX_CELLS:
            old = p.cells.pop(0)
            p.stack.children.pop(0)
            self.cell_of.pop(old.window, None)
            if p.edit_cell is old:
                p.edit_cell = p.input_cell
        if hasattr(self, "app"):  # not yet while the last run is restored at start
            self.app.invalidate()

    def _append_cell(self, s: Session, cell: Cell) -> None:
        """Add a cell at the bottom of its page, after every run: it becomes the bottom (input)
        cell, unless that one is still empty and unrun, which then stays the bottom one."""
        p = cell.page
        bottom = p.input_cell
        if bottom is not None and not bottom.buffer.text.strip() and not bottom.has_run:
            self._insert_cell(s, cell)
            return
        p.cells.append(cell)
        p.stack.children.append(cell.container)
        p.input_cell = cell
        while len(p.cells) > MAX_CELLS:
            old = p.cells.pop(0)
            p.stack.children.pop(0)
            self.cell_of.pop(old.window, None)
            if p.edit_cell is old:
                p.edit_cell = p.input_cell
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

    def split_cell(self, cell: Cell) -> None:
        """NORMAL Alt+S: a cell of several statements becomes one cell per statement, in order,
        on the same connection: the first stays this cell, the others go right below it. The
        focus stays on the first, at its start."""
        if cell is None or cell.buffer is None:
            return
        if cell.running:
            self.say("the cell is still running", error=True)
            return
        pieces = split_pieces(cell.buffer.text)
        if len(pieces) < 2:
            self.say("Alt+S: one statement in the cell, nothing to split", error=True)
            return
        p = cell.page
        # a page holds MAX_CELLS cells: the split never pushes out others (nor its own first
        # pieces, which it once did, losing them and failing on the focus); what does not fit
        # stays together in the last cell
        room = MAX_CELLS - len(p.cells) + 1  # this cell is the first piece
        if room < 2:
            self.say(f"Alt+S: the page holds {MAX_CELLS} cells already (Ctrl-D deletes one, or a new page: Ctrl-T)",
                     error=True)
            return
        whole = len(pieces)
        if whole > room:
            pieces = pieces[:room - 1] + ["\n".join(pieces[room - 1:])]
        cell.buffer.set_document(Document(pieces[0], 0), bypass_readonly=True)
        at = p.cells.index(cell)
        made = []
        for text in pieces[1:]:
            new = Cell(self, cell.session)
            new.page, new.conn_index = p, cell.conn_index
            new.bind_values = dict(cell.bind_values)  # the bind values the cell had
            new.buffer.set_document(Document(text, 0), bypass_readonly=True)
            at += 1
            p.cells.insert(at, new)
            p.stack.children.insert(at, new.container)
            made.append(new)
        if cell is p.input_cell:  # the bottom cell was split: the last piece is the bottom now
            p.input_cell = made[-1]
        self._focus_cell(cell, at_end=False)
        if whole > len(pieces):
            self.say(f"split into {len(pieces)} cells: a page holds {MAX_CELLS}, so the last one keeps the other "
                     f"{whole - len(pieces) + 1} statements together (Ctrl+Shift+O runs them all)")
        else:
            self.say(f"split into {len(pieces)} cells")
        self.app.invalidate()

    def duplicate_cell(self, cell: Cell) -> None:
        """NORMAL Shift+P: a copy of the cell's text right below it, on the same connection,
        focused with the cursor where it was (vim's "put before the cursor" gives way)."""
        if not cell.buffer.text.strip():
            self.say("P: the cell is empty, nothing to copy", error=True)
            return
        text, at = cell.buffer.text, cell.buffer.cursor_position
        self.new_cell_below(cell)
        copy = self._focused_cell()
        copy.conn_index = cell.conn_index
        copy.bind_values = dict(cell.bind_values)  # its bind values too
        copy.buffer.set_document(Document(text, at), bypass_readonly=True)
        if self.vi:
            self.app.vi_state.input_mode = InputMode.NAVIGATION
        self.say(f"cell copied below (conn{cell.conn_index})")

    # -- pages: the shell's tabs, each a stack of cells of its own -------------
    def _page_tabs(self):
        """The tab line above the shell: each page (a click shows it), then `+` for a new one."""
        s = self._session
        out = []
        for i, p in enumerate(s.pages):
            busy = " …" if any(c.running for c in p.cells) else ""
            out.append(("class:page.active" if i == s.page_index else "class:page", f" {i + 1} {p.name}{busy} ",
                        clicked(lambda i=i: self.show_page(i))))
            out.append(("class:border", "│"))
        out.append(("class:page.new", " + ", clicked(self.new_page)))
        out.append(("class:list.note", "  Ctrl-T new · Alt+←/→ switch"))
        return out

    def show_page(self, i: int) -> None:
        """Show page i: its cells in the shell, its last result below, the focus where it was."""
        s = self._session
        if not 0 <= i < len(s.pages):
            return
        s.page_index = i
        cell = s.edit_cell if s.edit_cell in s.cells and s.edit_cell.buffer is not None else s.input_cell
        self.app.layout.focus(cell.window)
        self.say(f"page {i + 1}/{len(s.pages)}: {s.page.name}")
        self.app.invalidate()

    def step_page(self, delta: int) -> None:
        s = self._session
        if len(s.pages) > 1:
            self.show_page((s.page_index + delta) % len(s.pages))

    def new_page(self) -> None:
        """Ctrl-T: an empty page right after the one shown, shown, its cell in INSERT mode."""
        s = self._session
        taken = {p.name for p in s.pages}
        name = next(f"page{n}" for n in range(len(s.pages) + 1, 10 ** 6) if f"page{n}" not in taken)
        self._new_page(s, name, at=s.page_index + 1)
        self.show_page(s.page_index)
        if self.vi:
            self.app.vi_state.input_mode = InputMode.INSERT

    def close_page(self) -> None:
        """Close the page shown, its cells with it ([ui] close_page = "confirm": asked first,
        when it holds any). The statements it ran stay in the History tab."""
        s = self._session
        page = s.page
        if len(s.pages) == 1:
            self.say("the last page stays (empty its cells instead)", error=True)
            return
        if any(c.running for c in page.cells):
            self.say(f"page {page.name}: a statement is still running", error=True)
            return
        filled = [c for c in page.cells if c.buffer is not None and (c.buffer.text.strip() or c.has_run)]
        if self.close_page_mode == "confirm" and filled:
            n = len(filled)
            self._ask_for(f"close {page.name} ({n} cell{'s' if n != 1 else ''})? y/n", "",
                          lambda a: None if a.lower() in ("y", "n", "yes", "no") else "y or n",
                          lambda a: self._drop_page(s, page) if a.lower().startswith("y") else self.say("kept"),
                          shell=True)
            return
        self._drop_page(s, page)

    def _drop_page(self, s: Session, page: Page) -> None:
        if page not in s.pages or len(s.pages) == 1:
            return
        for c in page.cells:
            self._drop_pager(c)
            self.cell_of.pop(c.window, None)
        s.pagers = [c for c in s.pagers if c.page is not page]
        i = s.pages.index(page)
        s.pages.pop(i)
        s.page_index = min(i, len(s.pages) - 1)
        self.show_page(s.page_index)
        self.say(f"page {page.name} closed")

    def clear_page(self) -> None:
        """Every cell of the page shown goes, one empty cell stays (the statements they ran
        stay in History). Not while one of them runs."""
        s = self._session
        page = s.page
        if any(c.running for c in page.cells):
            self.say(f"page {page.name}: a statement is still running", error=True)
            return
        for c in page.cells:
            self._drop_pager(c)  # a large result's open cursor closes
            self.cell_of.pop(c.window, None)
        s.pagers = [c for c in s.pagers if c.page is not page]
        fresh = Cell(self, s)
        page.cells = [fresh]
        page.stack.children[:] = [fresh.container]
        page.input_cell = page.edit_cell = fresh
        page.last_run = page.shown = None
        self._pane_hold = None
        self._focus_cell(fresh, at_end=True)
        if self.vi:
            self.app.vi_state.input_mode = InputMode.INSERT
        self.say(f"page {page.name} cleared: one empty cell")
        self.app.invalidate()

    def rename_page(self) -> None:
        s = self._session
        page = s.page

        def done(name: str) -> None:
            page.name = name
            self.say(f"page renamed: {name}")
        self._ask_for("page name:", page.name, lambda a: None if a else "a page needs a name", done, shell=True)

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
        if cell.buffer.text or cell.running or cell not in cell.session.cells:
            return
        if self._drop_cell(cell):
            self.say("empty cell removed")

    def delete_cell(self, cell: Cell | None) -> None:
        """Ctrl-D in a cell (or on its bind lines): the cell goes, whatever it holds; the focus
        moves to the next one (the one above for the bottom cell). The page's last cell stays,
        emptied. The statement it ran stays in History."""
        if cell is None or cell.buffer is None:
            return
        if cell.running:
            self.say("the cell is still running: Ctrl-C stops it first", error=True)
            return
        if len(self._editable(cell.session)) < 2:  # the last cell of the page stays, emptied
            cell.buffer.reset()
            self._clear_result(cell)
            self._focus_cell(cell, at_end=False)
            if self.vi:
                self.app.vi_state.input_mode = InputMode.INSERT
            self.say("the last cell stays: emptied")
            return
        if self._drop_cell(cell):
            self.say("cell deleted (its statement stays in History)")

    def _drop_cell(self, cell: Cell) -> bool:
        """Take the cell off its page; the focus on the next one (the one above for the bottom
        cell, which is the bottom one then). False when it is the last one left."""
        s = cell.session
        cells = self._editable(s)
        if len(cells) < 2 or cell not in cells:
            return False
        k = cells.index(cell)
        after = cells[k + 1] if k + 1 < len(cells) else cells[k - 1]
        i = s.cells.index(cell)
        s.cells.pop(i)
        s.stack.children.pop(i)
        self.cell_of.pop(cell.window, None)
        self.bind_of.pop(getattr(cell, "bind_window", None), None)
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
        if self._pane_hold is not None and cell in self._pane_hold:
            self._pane_hold = None
        self._focus_cell(after, at_end=False)  # at the start: a held backspace stops there
        self.app.invalidate()
        return True

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

    def _cell_in_focus(self) -> Cell | None:
        """The cell the focus is in: on its SQL or on its bind lines, which belong to it (so the
        result pane and the Trace tab show that cell's run)."""
        cell = self._focused_cell()
        if cell is None:
            c = self.bind_of.get(get_app().layout.current_window)
            cell = c if c is not None and c.session is self._session and c in self._session.cells else None
        return cell

    def _focused_cell(self) -> Cell | None:
        cell = self.cell_of.get(get_app().layout.current_window)
        return cell if cell is not None and cell.session is self._session else None

    def _editable(self, s: Session) -> list[Cell]:
        return [c for c in s.cells if c.buffer is not None]

    def _focus_cell(self, cell: Cell, at_end: bool) -> None:
        self.app.layout.focus(cell.window)
        cell.buffer.cursor_position = len(cell.buffer.text) if at_end else 0

    def _jump_cell(self, delta: int) -> None:
        """vim Ctrl-J (1) / Ctrl-K (-1): the next / previous cell, wherever the cursor is in this
        one, at its start. From a cell's bind lines, Ctrl-K is that cell's SQL (the lines are
        its), Ctrl-J the next cell."""
        on_binds = self.app.layout.current_window in self.bind_of
        cell = self._cell_in_focus()
        if cell is None:
            return
        if on_binds and delta < 0:
            self._focus_cell(cell, at_end=False)
            return
        cells = self._editable(cell.session)
        i = cells.index(cell) + delta
        if 0 <= i < len(cells):
            self._focus_cell(cells[i], at_end=False)

    def page_focused(self, direction: int) -> None:
        """PageUp (-1) / PageDown (1): only the pane with the focus scrolls a screen: the shell
        (from a cell or its bind lines: its view, the focused cell staying), the result pane, else
        the panel."""
        current = self.app.layout.current_window
        cell = self._focused_cell() or self.bind_of.get(current)
        if cell is not None:
            cell.page.container.page(direction)  # the view moves; the cursor, so what shows below, stays
        elif current is self.results.window:
            self.results.page(direction)
        elif self.show_panel:
            self.panel.page(direction)

    def _step(self, cell: Cell, delta: int, from_binds: bool = False) -> None:
        """Move focus to the previous/next editable cell (↑ on the first line, ↓ on the last),
        through a cell's bind lines, which sit under its SQL: down from the SQL goes onto them,
        up from the next cell lands on them."""
        if cell is None:
            return
        if delta > 0 and self.binds_shown(cell) and not from_binds:
            self.app.layout.focus(cell.bind_window)
            cell.bind_buffer.cursor_position = 0
            return
        cells = self._editable(cell.session)
        i = cells.index(cell) + delta
        if 0 <= i < len(cells):
            target = cells[i]
            if delta < 0 and self.binds_shown(target):
                self.app.layout.focus(target.bind_window)
                doc = target.bind_buffer.document
                target.bind_buffer.cursor_position = doc.translate_row_col_to_index(doc.line_count - 1, 0)
                return
            self._focus_cell(target, at_end=delta < 0)

    def _sync_results(self, _app=None) -> None:
        """Before each render: show the result of the focused cell in the bottom pane.

        On the input cell that is the latest run; with focus in the panel or the result
        pane itself, the pane keeps what it shows.
        """
        s = self._session
        cell = self._cell_in_focus()
        if cell is not None:
            s.edit_cell = cell
            hold = self._pane_hold
            if hold is not None and hold[0] is cell and hold[1] in s.cells:
                s.shown = hold[1]  # Ctrl-O went on to this cell: the run just made stays shown
            else:
                self._pane_hold = None
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
        h = s.history
        # kept until the history or the day changes: drawn on every redraw, a long history made
        # each key slow (the stamps only tell today from earlier days)
        key = (id(h), len(h), h[0][0] if h else None, h[-1][0] if h else None, len(s.runs), now.date())
        if self._history_cache is not None and self._history_cache[0] == key:
            return self._history_cache[1]
        digits = len(str(len(s.runs)))  # run numbers, right-aligned: 1 is the oldest run
        items = [([("class:list.id", f"{n:>{digits}} "), ("class:list.note", f"{history_stamp(when, now)} ")]
                  + highlighted(" ".join(text.split())), "", "")
                 for n, when, text in h]
        self._history_cache = (key, items)
        return items

    # -- bookmark: saved queries with a name; one with an `@alias` is a template too ------------
    # -- Archive: bookmarks, templates (`@name args`) and macros (`(name args)`), a page each ----
    ARCHIVE_HEAD = 2  # rows above the first entry: the pages' bar, the keys
    ARCHIVE_TITLES = {"bookmark": "Bookmark", "template": "Template", "macro": "Macro"}
    ARCHIVE_KEYS = {
        "bookmark": "Enter / i SQL into the prompt · a/A add · e edit · r memo · M move · d y",
        "template": "Enter SQL · i @call · a/A add · e edit · r memo · t name · M move · d y · $1 text :1 bind",
        "macro": "Enter SQL · i (call) · a/A add · e edit · r memo · t name · M move · d y · $1 text :1 bind",
    }
    ARCHIVE_EMPTY = {
        "bookmark": "(none: a adds one, A bookmarks the current cell)",
        "template": "(none: a adds one, A makes the current cell one; called as @name args)",
        "macro": "(none: a adds one, A makes the current cell one; called alone in a cell as (name args))",
    }

    def go_to_archive(self, kind: str) -> None:
        """The panel on the Archive tab's `kind` page (Alt+M: the macros), the focus on it."""
        self.show_panel = True
        self.panel.select(PANEL_TABS.index("archive"))
        self._archive_page(0, kind)
        self.app.layout.focus(self.panel.window)
        if self.vi:
            self.app.vi_state.input_mode = InputMode.NAVIGATION
        self.say(f"Archive: {self.ARCHIVE_TITLES[kind]} page · Enter puts the query, i the call ((name ))")

    def _archive_page(self, delta: int, kind: str | None = None) -> None:
        """`[` / `]` (or a click on the bar): the previous / next page of the Archive tab."""
        kinds = list(KINDS)
        self.archive_kind = kind or kinds[(kinds.index(self.archive_kind) + delta) % len(kinds)]
        self._tpl_delete = None
        self.panel.tabs[PANEL_TABS.index("archive")].cursor = self.ARCHIVE_HEAD
        self.app.invalidate()

    def _template_items(self):
        kind = self.archive_kind
        entries = self.templates.entries(kind)
        bar = []
        for k in KINDS:
            if bar:
                bar.append(("class:border", "│"))
            bar.append(("class:tab.active" if k == kind else "class:tab", f" {self.ARCHIVE_TITLES[k]} ",
                        clicked(lambda k=k: self._archive_page(0, k))))
        bar.append(("class:list.note", "   [ / ] page"))
        head = [(bar, "", ""), ([("class:list.note", self.ARCHIVE_KEYS[kind])], "", "")]
        if not entries:
            return head + [(self.ARCHIVE_EMPTY[kind], "class:list.empty", "")]
        idw = len(str(max(t.id for t in entries)))
        aw = max((len(t.call) + 1 for t in entries if t.call), default=0)
        room = self.panel.width() - 3  # the panel draws a space before and after each line, and the scrollbar
        out = head
        for t in entries:
            # id, the call (@name / (name)) and the query from the left; the memo at the right end
            name = fit(t.memo, max(8, room // 3)) if t.memo else ""
            frags = [("class:list.id", f"{t.id:>{idw}} ")]
            if aw:
                frags.append(("class:prompt.num", f"{t.call:<{aw}}"))
            used = sum(get_cwidth(x) for _, x in frags)
            query = cut_fragments(highlighted(" ".join(t.body.split())), room - used - get_cwidth(name) - 2)
            used += sum(get_cwidth(x) for _, x in query)
            gap = " " * max(2 if name else 0, room - used - get_cwidth(name))
            out.append((frags + query + [("", gap), ("class:schema.title", name)], "", ""))
        return out

    def _template_current(self):
        """The entry on the panel's cursor line (below the pages' bar and the keys)."""
        i = self.panel.tab.cursor - self.ARCHIVE_HEAD
        entries = self.templates.entries(self.archive_kind)
        return entries[i] if 0 <= i < len(entries) else None

    def _template_select(self, t) -> None:
        """The Archive tab on `t`'s page, its cursor on it."""
        self.archive_kind = t.kind
        ids = [x.id for x in self.templates.entries(t.kind)]
        if t.id in ids:
            self.panel.tabs[PANEL_TABS.index("archive")].cursor = self.ARCHIVE_HEAD + ids.index(t.id)

    def _template_pick(self, i: int) -> None:  # Enter (or a click) on a line: its SQL into the prompt
        if i >= self.ARCHIVE_HEAD:
            self._template_put(as_call=False)

    def _template_edit_current(self) -> None:
        t = self._template_current()
        if t is None:
            return
        self._tpl_delete = None
        self._open_editor(t.id, t.memo, t.alias, t.body, t.kind)

    def _template_ask_alias(self, done: Callable[[str | None], None], text: str = "",
                            keep: int | None = None, kind: str = "template") -> None:
        """A template's @name or a macro's name (needed for both)."""
        label = "@name (called as @name args):" if kind == "template" else "macro name (called as (name args)):"
        strip = lambda a: a.strip().lstrip("@").strip("()").strip()
        self._ask_for(label, text,
                      lambda a: "a name is needed" if not strip(a)
                      else self.templates.check_alias(strip(a), keep=keep, kind=kind),
                      lambda a: done(strip(a)))

    def _template_ask_memo(self, done: Callable[[str], None], text: str = "") -> None:
        self._ask_for("memo (optional):", text, lambda m: None, done)

    def _with_name(self, kind: str, done: Callable[[str | None], None]) -> None:
        """A bookmark needs no name: on at once; a template or a macro asks for it."""
        if kind == "bookmark":
            done(None)
        else:
            self._template_ask_alias(done, kind=kind)

    def _template_add(self) -> None:
        """`a`: an optional memo (and a template's or macro's name), then the query in the editor."""
        self._tpl_delete = None
        kind = self.archive_kind

        def got_memo(memo: str) -> None:
            self._with_name(kind, lambda alias: self._open_editor(None, memo, alias, "", kind))
        self._template_ask_memo(got_memo)

    def _template_add_cell(self, cell: Cell | None = None) -> None:
        """`A` in the Archive tab, Ctrl-R in a cell: the cell's SQL (the cell last edited for
        `A`) kept on the Archive page shown, asking for an optional memo (and a name) under the
        panel."""
        self._tpl_delete = None
        cell = cell or self._target_cell()
        body = cell.buffer.text.strip() or (cell.ran_text or "").strip()
        if not body:
            self.say("the cell is empty: nothing to keep", error=True)
            return
        kind = self.archive_kind

        def got_memo(memo: str) -> None:
            def got_alias(alias: str | None) -> None:
                try:
                    t = self.templates.add(body, memo, alias, kind=kind)
                except TemplateError as e:
                    self.say(str(e), error=True)
                    return
                self._template_select(t)
                self.say(f"kept as {self.ARCHIVE_TITLES[kind].lower()} #{t.id}" + (f" {t.call}" if t.call else "")
                         + (f": {t.memo}" if t.memo else ""))
            self._with_name(kind, got_alias)
        self.show_panel = True  # the questions open under the panel, on the Archive tab
        self.panel.select(PANEL_TABS.index("archive"))
        self._template_ask_memo(got_memo)

    def _template_rename(self) -> None:
        """`r`: change the memo (empty: none)."""
        t = self._template_current()
        if t is None:
            return
        self._tpl_delete = None

        def got(memo: str) -> None:
            if memo != t.memo:
                self.templates.update(t.id, memo=memo)
                self.say(f"#{t.id}: memo " + (f"is {memo!r} now" if memo else "cleared"))
        self._template_ask_memo(got, t.memo)

    def _template_alias(self) -> None:
        """`t`: a template's @name or a macro's name, changed."""
        t = self._template_current()
        if t is None:
            return
        self._tpl_delete = None
        if t.kind == "bookmark":
            self.say("a bookmark has no name: M moves it to Template or Macro", error=True)
            return

        def got(alias: str | None) -> None:
            if alias == t.alias:
                return
            self.templates.update(t.id, alias=alias)
            moved = next(x for x in self.templates.entries() if x.id == t.id)
            self.say(f"#{t.id} is {moved.call} now")
        self._template_ask_alias(got, t.alias or "", keep=t.id, kind=t.kind)

    def _template_move(self) -> None:
        """`M`: move the entry to another page (b, t, m); a template or a macro gets a name."""
        t = self._template_current()
        if t is None:
            return
        self._tpl_delete = None
        letters = {"b": "bookmark", "t": "template", "m": "macro"}

        def to(kind: str) -> None:
            def done(alias: str | None) -> None:
                try:
                    self.templates.update(t.id, alias=alias, kind=kind)
                except TemplateError as e:
                    self.say(str(e), error=True)
                    return
                moved = next(x for x in self.templates.entries() if x.id == t.id)
                self._template_select(moved)
                self.say(f"#{t.id} moved to {self.ARCHIVE_TITLES[kind]}" + (f": {moved.call}" if moved.call else ""))
            if kind == "bookmark":
                done(None)
            elif t.alias and not self.templates.check_alias(t.alias, keep=t.id, kind=kind):
                done(t.alias)  # its name serves there too
            else:
                self._template_ask_alias(done, t.alias or "", keep=t.id, kind=kind)
        others = "/".join(k for k, v in letters.items() if v != t.kind)
        self._ask_for(f"move #{t.id} to ({others}: bookmark, template, macro):", "",
                      lambda a: None if letters.get(a.strip().lower()[:1]) not in (None, t.kind)
                      else f"{others}: the page to move it to",
                      lambda a: to(letters[a.strip().lower()[:1]]))

    def _template_delete(self) -> None:
        t = self._template_current()
        if t is None:
            return
        if self._tpl_delete != t.id:
            self._tpl_delete = t.id
            self.say(f"d again deletes {t.label}")
            return
        self._tpl_delete = None
        self.templates.remove(t.id)
        self.say(f"{t.label} deleted")

    def _template_insert(self) -> None:
        """`i`: the call at the prompt: `@name ` for a template (arguments next, then Tab
        expands it, or Ctrl-O runs it), `(name )` alone in a cell for a macro; the SQL itself
        for a bookmark."""
        self._template_put(as_call=True)

    def _template_put(self, as_call: bool) -> None:
        t = self._template_current()
        if t is None:
            return
        cell = self._target_cell()
        buf = cell.buffer
        before = buf.document.text_before_cursor
        if as_call and t.kind == "macro" and t.alias:
            if buf.text.strip():  # a macro stands alone in its cell: a new one below
                self.new_cell_below(cell)
                cell = self._focused_cell()
                buf = cell.buffer
            call = f"({t.alias} )"
            buf.set_document(Document(call, len(call) - 1))  # the cursor where the arguments go
        elif as_call and t.kind == "template" and t.alias:
            buf.insert_text(("" if not before or before[-1] in " \t\n(,=" else " ") + f"@{t.alias} ")
        elif not buf.text.strip():
            buf.set_document(Document(t.body, len(t.body)))
        else:  # the cell has text: the query goes on a line of its own at the cursor
            buf.insert_text(("" if not before or before.endswith("\n") else "\n") + t.body)
        self.app.layout.focus(cell.window)
        if self.vi:
            self.app.vi_state.input_mode = InputMode.INSERT

    def _template_copy(self) -> None:
        t = self._template_current()
        if t is not None:
            self.copy_to_clipboard(t.body, t.label)

    def _ask_for(self, label: str, text: str, check: Callable[[str], str | None],
                 done: Callable[[str], None], cancel: Callable[[], None] | None = None,
                 shell: bool = False) -> None:
        """A one-line question under the panel (`shell`: under the shell): Enter answers (when
        `check` finds nothing wrong), Esc / Ctrl-C drops it (and calls `cancel`, as does another
        question taking its place)."""
        if self._ask is not None and self._ask.get("cancel"):
            self._ask["cancel"]()
        self._ask = {"label": label, "check": check, "done": done, "cancel": cancel, "shell": shell,
                     "back": self.app.layout.current_window}
        self.ask_buffer.set_document(Document(text, len(text)))
        self.app.layout.focus(self.shell_ask_window if shell else self.ask_window)
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
        # back where it was asked from: the editor, a cell (Ctrl-R), else the panel
        in_cell = back in self.cell_of and self.cell_of[back].session is self._session
        self.app.layout.focus(back if (back is self.editor_window and self._tpl_edit) or in_cell else self.panel.window)
        if self.vi:
            self.app.vi_state.input_mode = InputMode.NAVIGATION
        if answer is None:
            self.say("")
            if ask.get("cancel"):
                ask["cancel"]()
        else:
            ask["done"](answer)

    def _open_editor(self, id: int | None, memo: str, alias: str | None, body: str,
                     kind: str = "bookmark") -> None:
        """Edit an Archive entry's query in the panel (a new one when `id` is None), in INSERT mode."""
        call = f"@{alias}" if kind == "template" and alias else f"({alias})" if kind == "macro" and alias else ""
        label = memo or call or kind
        self._tpl_edit = {"id": id, "memo": memo, "alias": alias, "kind": kind, "call": call,
                          "label": label, "saved": body}
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
        kind = self.ARCHIVE_TITLES.get(e.get("kind", "bookmark"), "Bookmark")
        title = f" {kind}" + (f" {e['memo']}" if e["memo"] else "") + (f" {e['call']}" if e.get("call") else "") + \
            (" (new)" if e["id"] is None else f" #{e['id']}") + (" [+]" if changed else "")
        args = " · $1 text, :1 bound (the arguments)" if e.get("kind") in ("template", "macro") else ""
        return [("class:tab.active.focused", title + " "),
                ("class:list.note", f" :w save · :wq save+close · :q close · :q! drop{args} ")]

    def _editor_save(self) -> bool:
        e, body = self._tpl_edit, self.editor_buffer.text.rstrip()
        if not body.strip():
            self.say(f"{e['label']}: an empty query is not saved (:q! drops it; d deletes a bookmark)", error=True)
            return False
        try:
            if e["id"] is None:
                t = self.templates.add(body, e["memo"], e["alias"], kind=e.get("kind", "bookmark"))
                e["id"] = t.id
                self.say(f"{e['label']} added")
            else:
                self.templates.update(e["id"], body=body)
                self.say(f"{e['label']} saved")
        except TemplateError as err:
            self.say(str(err), error=True)
            return False
        e["saved"] = self.editor_buffer.text
        return True

    def _editor_close(self) -> None:
        e = self._tpl_edit
        self._tpl_edit = None
        t = next((t for t in self.templates.entries() if e and t.id == e["id"]), None)
        if t is not None:
            self._template_select(t)  # the edited line, on its page
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
            self.say(f"{e['label']}: " + ("not added" if e["id"] is None else "changes dropped"))
        elif cmd:
            self.say(f"not an editor command: :{cmd} (w, wq, x, q, q!)", error=True)

    # -- settings: several settings in one tab; for now the color theme ------------------
    EDITING_MODES = {"vi": "vim keys: NORMAL / INSERT / VISUAL (the default)",
                     "emacs": "emacs keys: Ctrl-A / Ctrl-E, Ctrl-K …, always typing"}
    CLOSE_PAGE = {"delete": "a page closes at once, its cells with it (the default)",
                  "confirm": "asks first when the page holds cells"}
    COMPLETION_CASE = {"upper": "Tab completes in upper case: sel -> SELECT (the default)",
                       "typed": "Tab follows your typing: sel -> select, SEL -> SELECT"}
    HISTORY_SIZES = (100, 500, 1000, 5000)  # the Settings tab's choices (config.toml takes any 10..100000)

    def _settings_rows(self) -> list[tuple[str, str]]:
        """Each line's kind: "head" | "section" | "theme" | "mode" | "close" | "case" | "gap", and its value."""
        return ([("head", ""), ("section", "Theme")] + [("theme", name) for name in THEMES]
                + [("gap", ""), ("section", "Editing mode")] + [("mode", m) for m in self.EDITING_MODES]
                + [("gap", ""), ("section", "Completion case")] + [("case", m) for m in self.COMPLETION_CASE]
                + [("gap", ""), ("section", "Closing a page")] + [("close", m) for m in self.CLOSE_PAGE]
                + [("gap", ""), ("section", "History size")] + [("history", str(n)) for n in self._history_sizes()])

    def _history_sizes(self) -> list[int]:
        return sorted(set(self.HISTORY_SIZES) | {self.history_max})  # a size set in config.toml too

    def _settings_items(self):
        out = []
        width = max([len(t.title) for t in THEMES.values()] + [len(m) for m in self.EDITING_MODES]
                    + [len(m) for m in self.CLOSE_PAGE]) + 2
        mark = lambda on: ("class:list.active" if on else "class:list.note", " ● " if on else " ○ ")
        for kind, value in self._settings_rows():
            if kind == "head":
                out.append(([("class:schema.title", "Settings"),
                             ("class:list.note", "  Enter applies · saved to config.toml [ui]")], "", ""))
            elif kind == "section":
                out.append(([("class:schema.kind", value)], "", ""))
            elif kind == "gap":
                out.append(("", "", ""))
            elif kind == "theme":
                t, on = THEMES[value], value == self.theme
                frags = [mark(on), ("class:schema.title" if on else "", f"{t.title:<{width}}")]
                frags += [(f"class:swatch.{t.name}.{k}", "  ") for k in SWATCH]
                frags.append(("class:list.note", f"  {t.about}"))
                out.append((frags, "", "default" if t.name == DEFAULT_THEME else ""))
            elif kind == "close":
                on = value == self.close_page_mode
                frags = [mark(on), ("class:schema.title" if on else "", f"{value:<{width}}"),
                         ("class:list.note", self.CLOSE_PAGE[value])]
                out.append((frags, "", "default" if value == "delete" else ""))
            elif kind == "history":
                on = int(value) == self.history_max
                frags = [mark(on), ("class:schema.title" if on else "", f"{value:<{width}}"),
                         ("class:list.note", f"the History tab keeps the latest {value} statements")]
                out.append((frags, "", "default" if value == "500" else ""))
            elif kind == "case":
                on = (value == "upper") == self.words.upper
                frags = [mark(on), ("class:schema.title" if on else "", f"{value:<{width}}"),
                         ("class:list.note", self.COMPLETION_CASE[value])]
                out.append((frags, "", "default" if value == "upper" else ""))
            else:
                on = (value == "vi") == self.vi
                frags = [mark(on), ("class:schema.title" if on else "", f"{value:<{width}}"),
                         ("class:list.note", self.EDITING_MODES[value])]
                out.append((frags, "", "default" if value == "vi" else ""))
        return out

    def _settings_pick(self, i: int) -> None:
        rows = self._settings_rows()
        if not 0 <= i < len(rows):
            return
        kind, value = rows[i]
        if kind == "theme":
            self.set_theme(value)
        elif kind == "mode":
            self.set_editing_mode(value)
        elif kind == "close":
            self.close_page_mode = value
            self.app.invalidate()
            self._save_ui("close_page", value, f"closing a page: {value}")
        elif kind == "history":
            self.set_history_max(int(value))
        elif kind == "case":
            self.words.upper = value == "upper"
            self.app.invalidate()
            self._save_ui("completion_case", value, f"completion case: {value}")

    def set_history_max(self, n: int) -> None:
        """The History tab keeps the latest `n` statements (each once), from now on and on the
        next start ([ui] history_max); the history file keeps every run."""
        self.history_max = n
        for s in self.sessions.values():
            s.history = history_view(s.runs, n)
        self.app.invalidate()
        self._save_ui("history_max", n, f"history size: {n}")

    def _save_ui(self, key: str, value, shown: str) -> None:
        """Keep a setting for the next start ([ui] in config.toml), and say it is on."""
        try:
            save_ui_setting(self.ai_settings.config_path or CONFIG_PATH, key, value)
        except (OSError, ConnectionError_) as e:
            self.say(f"{shown} on, but not saved: {e}", error=True)
            return
        self._ai_mtime = self._config_mtime()  # our own write: nothing for the AI tab to reload
        self.say(shown)

    def set_theme(self, name: str) -> None:
        """Draw with the theme now, and keep it for the next start ([ui] theme in config.toml)."""
        if name not in THEMES:
            return
        self.theme, self.style, self.flash_colors = name, theme_style(name), flash_colors(name)
        self.app.invalidate()
        self._save_ui("theme", name, f"theme: {THEMES[name].title}")

    def set_editing_mode(self, mode: str) -> None:
        """vi or emacs keys from now on in every input (cells, the editors, the question lines),
        and on the next start ([ui] editing_mode)."""
        if mode not in self.EDITING_MODES:
            return
        self.vi = mode == "vi"
        self.app.editing_mode = EditingMode.VI if self.vi else EditingMode.EMACS
        if self.vi:  # the focus is in this tab: back in a cell, NORMAL mode, as at the start of vim
            self.app.vi_state.input_mode = InputMode.NAVIGATION
        self.app.invalidate()
        self._save_ui("editing_mode", mode, f"editing mode: {mode}")

    # -- AI: questions about SQL to Claude, with this session's context ---------------------
    # no key yet: the subscriptions to pick from, or how to add one
    AI_PICK_HEAD = 3  # rows above the first subscription in the picker

    def _config_mtime(self) -> float | None:
        try:
            return self.ai_settings.config_path.stat().st_mtime if self.ai_settings.config_path else None
        except OSError:
            return None

    def _ai_reload(self) -> None:
        """Take in a subscription added or picked meanwhile (`./dbcli subscription …`)."""
        mtime = self._config_mtime()
        if mtime == self._ai_mtime or self.ai_settings.config_path is None:
            return
        self._ai_mtime = mtime
        try:
            self.ai_settings = load_config(self.ai_settings.config_path).ai
        except Exception:  # a half-edited file: keep what was read before
            return
        self.assistant = Assistant(self.ai_settings.effective())

    def _ai_picking(self) -> bool:
        return not self.ai_settings.has_key() and not self._ai_anyway

    def _ai_picker_items(self):
        ai = self.ai_settings
        head = [([("class:schema.title", "AI"), ("class:list.note", "  no subscription is active")], "", "")]
        if not ai.subscriptions:
            return head + [(t, style, "") for t, style in (
                ("", ""),
                ("Add an Anthropic API key first, in a terminal:", "class:list.note"),
                ("  ./dbcli subscription add", "class:ai.you"),
                ("It asks for a name and the key (not shown).", "class:list.note"),
                ("This tab picks it up by itself.", "class:list.note"),
                ("", ""),
                ("(ANTHROPIC_API_KEY or an `ant auth login`", "class:list.note"),
                ("profile work too: a asks anyway)", "class:list.note"))]
        rows = head + [("Pick one: Enter uses it (saved as [ai] subscription)", "class:list.note", ""), ("", "", "")]
        for name, sub in ai.subscriptions.items():
            extra = " · ".join(x for x in (sub.model, sub.effort) if x)
            rows.append(([("class:ai.you", name), ("class:list.note", f"  {mask_key(sub.api_key)}")], "", extra))
        if ai.subscription:
            rows.append((f"([ai] subscription = {ai.subscription!r} is not defined)", "class:error", ""))
        return rows

    def _ai_pick(self, i: int) -> None:
        """Enter in the AI tab: use the subscription under the cursor (picker), else ask."""
        if not self._ai_picking():
            self._ai_prompt()
            return
        names = list(self.ai_settings.subscriptions)
        k = i - self.AI_PICK_HEAD
        if not 0 <= k < len(names):
            if not names:
                self.say("no subscription yet: ./dbcli subscription add (in a terminal)", error=True)
            return
        try:
            subscription_activate(self.ai_settings.config_path, names[k])
        except Exception as e:  # the file could not be changed: say why
            self.say(f"could not save it: {e}", error=True)
            return
        self._ai_reload()
        self.panel.tab.cursor = 0
        self.say(f"AI uses subscription {names[k]} now")

    def _ai_items(self):
        self._ai_reload()
        if self._ai_picking():
            return self._ai_picker_items()
        chat, width = self._session.chat, max(20, self.panel.width() - 4)
        st = self.assistant.settings
        head = [([("class:schema.title", "AI"), ("class:list.note", f"  {st.model} · Enter ask · i insert SQL · y copy · c new chat · Ctrl-C stop")],
                 "", "")]
        if not chat.turns:
            return head
        out = list(head)
        for turn in chat.turns:
            out.append(("", "", ""))
            for k, line in enumerate(wrap_text(turn.question, width - 2) or [""]):
                out.append(([("class:ai.you", "▸ " if k == 0 else "  ")] + marked_refs(line, "class:ai.you"), "", ""))
            out += [(frags, "", "") for frags in answer_lines(turn.answer, width)]
            if turn.error:
                out.append((f"  {turn.error}", "class:error", ""))
            elif not turn.done:
                out.append(("  …", "class:list.note", ""))
        return out

    def _ai_follow(self) -> None:
        """Keep the newest line in view while an answer streams in."""
        tab = self.panel.tabs[PANEL_TABS.index("ai")]
        tab.cursor = max(0, len(self._ai_items()) - 1)
        self.app.invalidate()

    def _ai_prompt(self) -> None:
        self._ai_anyway = True  # asked for: show the chat, whatever key the SDK finds
        if self._session.chat.busy:
            self.say("still answering: Ctrl-C in the AI tab stops it", error=True)
            return
        self._ask_for(AI_PROMPT, "", self._ai_check, self._ai_send)

    def _ai_check(self, question: str) -> str | None:
        """Why the question cannot go yet: empty, or it names a cell that is not there."""
        if not question.strip():
            return "type a question first"
        n = len(self._editable(self._session))
        missing = [k for k in cell_refs(question) if k >= n]
        if missing:
            return f"no cell #c{missing[0]}: the cells are #c0 … #c{n - 1}"
        return None

    def _cell_outcome(self, cell: Cell) -> str:
        """A cell's last run for the AI: its result title and any error (never its rows)."""
        if not cell.has_run:
            return ""
        errors = "".join(t for st, t in cell.output if st == "class:error")
        return "\n".join(x for x in (cell.result_title, errors) if x)

    def _ai_send(self, question: str) -> None:
        s = self._session
        cell = self._target_cell()
        system = system_prompt(s.profile.type, s.columns, s.relations)
        cells = self._editable(s)
        named = [(k, cells[k].buffer.text, self._cell_outcome(cells[k])) for k in cell_refs(question) if k < len(cells)]
        content = user_message(question, cell.buffer.text if cell.buffer else "", self._cell_outcome(cell), cells=named)
        turn = Turn(question, sent=content)
        s.chat.turns.append(turn)
        s.chat.busy = True

        def on_text(text: str) -> None:
            turn.answer += text
            self._ai_follow()

        async def go():
            try:
                await self.assistant.ask(s.chat, system, content, on_text)
            except asyncio.CancelledError:
                turn.error = "stopped"
            except Exception as e:  # the SDK's errors, or no SDK: shown in the tab
                turn.error = error_message(e)
            finally:
                turn.done, s.chat.busy, self._ai_task = True, False, None
                s.chat.trim()  # the latest KEEP_TURNS only, the history rebuilt from them
                self._save_chat(s)
                self._ai_follow()
        self._ai_task = self.app.create_background_task(go())
        self._ai_follow()

    def _ai_last_answer(self) -> Turn | None:
        done = [t for t in self._session.chat.turns if t.answer]
        return done[-1] if done else None

    def _ai_insert(self) -> None:
        """The last answer's last SQL block at the cursor of the input area."""
        t = self._ai_last_answer()
        sql = last_sql(t.answer) if t else None
        if not sql:
            self.say("no SQL in the last answer", error=True)
            return
        cell = self._target_cell()
        before = cell.buffer.document.text_before_cursor
        cell.buffer.insert_text(("" if not before or before.endswith("\n") else "\n") + sql)
        self.app.layout.focus(cell.window)
        self.say("SQL from the AI tab inserted · u undoes")

    def _ai_copy(self) -> None:
        t = self._ai_last_answer()
        if t:
            self.copy_to_clipboard(t.answer, "the last answer")

    def _ai_clear(self) -> None:
        if self._session.chat.busy:
            self.say("still answering: Ctrl-C stops it first", error=True)
            return
        self._session.chat = Chat()
        self._save_chat(self._session)
        self.panel.tab.cursor = 0
        self.say("new AI chat")

    def _save_chat(self, s: Session) -> None:
        try:
            save_chat(s.folder, s.chat.to_json())
        except OSError as e:
            self.say(f"AI chat not saved: {e}", error=True)

    def _ai_stop(self) -> None:
        if self._ai_task is not None:
            self._ai_task.cancel()

    # -- monitor: sessions, blocking, locks, top SQL, waits; refreshed while it shows ---------
    def _monitor_items(self):
        """The last snapshot; a new one only when asked for (Ctrl-O here): dbcli sends nothing
        to the database on its own."""
        s, m = self._session, self._session.watch
        if s.db is None and not m.lines:
            return [("not connected", "class:list.note", "")]
        if m.loading:
            when = "loading…"
        elif m.when is None:
            when = "no snapshot yet"
        else:  # a snapshot of an earlier day (kept from the last run) shows its date
            when = m.when.strftime("%H:%M:%S" if m.when.date() == dt.date.today() else "%Y-%m-%d %H:%M:%S")
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
            self._save_monitor(s)
        except Exception as e:  # shown, not kept: the next start has the last good snapshot
            s.watch.lines = [[("class:error", f"  {error_text(e)}")]]
        finally:
            s.watch.loading = False
            self.app.invalidate()

    # -- the monitor tab's last snapshot outlives the terminal: sessions/<name>/monitor.json --
    def _save_monitor(self, s: Session) -> None:
        try:
            save_monitor(s.folder, {"lines": s.watch.lines,
                                    "when": s.watch.when.isoformat() if s.watch.when else None})
        except (OSError, TypeError, ValueError) as e:
            self.say(f"monitor tab not saved: {e}", error=True)

    def _load_monitor(self, s: Session) -> None:
        d = load_monitor(s.folder)
        if not d:
            return
        try:
            s.watch.lines = [[tuple(f) for f in line] for line in d.get("lines") or []]
            s.watch.when = dt.datetime.fromisoformat(d["when"]) if d.get("when") else None
        except (KeyError, TypeError, ValueError):
            s.watch = Watch()  # a damaged file: no snapshot

    def _count_key(self, _sender=None) -> None:
        """One more key (or mouse event) in: a view the shell's PageUp / wheel left goes back to
        following the cursor from the next key on (ShellPane)."""
        self._keys_in += 1

    def _show_cursor(self, _sender=None) -> None:
        """Any key brings back the cursor a run hid (Ctrl-O's own key comes before the run)."""
        for c in self._session.cells:
            c.cursor_hidden = False

    async def spin_loop(self) -> None:
        """Redraw while a cell runs, so its spinner turns, and while a run's text is lit (often
        enough that its pulse and fade look smooth); a mark that has faded out goes."""
        lit = False
        while True:
            await asyncio.sleep(FADE_INTERVAL if lit else SPIN_INTERVAL)
            now, redraw, lit = time.monotonic(), False, False
            for c in self._session.cells:
                if c.flash is not None:
                    if flash_level(c.flash["started"], c.flash["done"], now) is None:
                        c.flash, c.cursor_hidden = None, False
                    else:
                        lit = True
                    redraw = True  # each frame of the pulse, and the one where it goes
                redraw = redraw or c.running
            if redraw:
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
        name = self.panel.tab.name
        if name == "monitor":
            return max(PANEL_WIDTH, get_app().output.get_size().columns // 2)
        if name == "trace":
            return self._trace_width()
        return max(PANEL_WIDTH, min(TRACE_WIDTH, get_app().output.get_size().columns // 2))

    def _trace_width(self) -> int:
        """Half the screen, as the monitor. With DISPLAY_CURSOR's table on (`r`), as wide as
        that table, leaving the shell 40 columns."""
        columns = get_app().output.get_size().columns
        width = max(PANEL_WIDTH, columns // 2)
        shown = self._session.shown
        if self.trace_raw and shown is not None and shown.trace:
            need = trace_raw_width(shown.trace) + 4  # the line's spaces and the scrollbar
            width = max(width, min(need, columns - 40))
        return width

    def _trace_toggle_raw(self) -> None:
        self.trace_raw = not self.trace_raw
        self.say("trace: DISPLAY_CURSOR as its table" if self.trace_raw else "trace: plan as a tree")
        self.app.invalidate()

    def _trace_items(self):
        s, shown = self._session, self._session.shown
        if shown is not None and shown.trace:
            return [(frags, "", "") for frags in render_trace(shown.trace, raw=self.trace_raw)]
        if s.profile.type != "oracle":
            return [("Trace works on Oracle connections only", "class:list.note", "")]
        state = "autotrace on" if s.autotrace else "autotrace off"
        return [(state, "class:list.active" if s.autotrace else "class:list.note", ""), ("", "", ""),
                ("Turn it on with F8 (or click", "class:list.note", ""),
                ("autotrace in the top bar) and", "class:list.note", ""),
                ("run a query: its plans and", "class:list.note", ""),
                ("session statistics show here.", "class:list.note", "")]

    def _cycle_macro(self, buf: Buffer) -> bool:
        """Tab right after `(` at a cell's start (`(`, `(*`, `(*c`): the macros whose name starts
        so, one by one, each Tab the next (round), followed by a space, with the `)` of the call
        put after the cursor when there is none. False when it is no macro name's place, or none
        matches."""
        doc = buf.document
        c = self._macro_cycle
        if (c is not None and c["buffer"] is buf and doc.cursor_position == c["start"] + len(c["shown"])
                and doc.text[c["start"]:doc.cursor_position] == c["shown"]):
            c["i"] = (c["i"] + 1) % len(c["names"])  # Tab again: the next one
        else:
            m = MACRO_TYPED_RE.fullmatch(doc.text_before_cursor)
            if not m or doc.text_after_cursor.strip() not in ("", ")"):
                return False
            names = self.templates.macro_names(m.group(1))
            if not names:
                return False
            c = self._macro_cycle = {"buffer": buf, "start": m.start(1), "shown": m.group(1), "names": names, "i": 0}
        name = c["names"][c["i"]] + " "
        buf.delete_before_cursor(len(c["shown"]))
        buf.insert_text(name)
        c["shown"] = name
        if ")" not in buf.document.text_after_cursor:
            buf.insert_text(")", move_cursor=False)  # the call's end, after the arguments to come
        body = " ".join(self.templates._macros.get(c["names"][c["i"]], "").split())
        self.say(f"({c['names'][c['i']]}) {body}" + (f"   · Tab: the next of {len(c['names'])}" if len(c["names"]) > 1 else ""))
        return True

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
                items = [self.words.cased(prefix or hint, h) for h in self.words.hint_candidates(prefix)]
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
        left = re.search(r"([^\W\d][\w$#]*)\.([^\W\d][\w$#]*)\s*=\s*$", text)
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
        target = re.search(r"\bupdate\s+(?:only\s*\()?([^\W\d][\w$#.]*)\s*\)?(?:\s+[^\W\d][\w$#]*)?\s*$", head, re.I)
        if target is None:  # merge into t using … when matched then update set
            if not re.search(r"\bupdate\s*$", head, re.I):
                return []
            target = list(re.finditer(r"\bmerge\s+into\s+([^\W\d][\w$#.]*)", head, re.I))
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
        m = re.search(r"\binto\s+([^\W\d][\w$#.]*)(?:\s+[^\W\d][\w$#]*)?\s*\(([^()]*)$", text, re.I)
        if m:
            name, listed = m.group(1), m.group(2)
        else:  # merge into t using … when not matched then insert (
            m = re.search(r"\binsert\s*\(([^()]*)$", text, re.I)
            into = list(re.finditer(r"\bmerge\s+into\s+([^\W\d][\w$#.]*)", text, re.I))
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
        """The result pane's title, on its heavy rule: the row count alone, bold (`14 rows`,
        `500 rows, more ↓`), and the selected row while the pane has the focus. The statement
        and the connection are in the shell above and the top bar."""
        s = self._session
        focused = self.results.has_focus()
        shown = s.shown
        out = [("class:result.rule", "━━")]
        count = ""
        if shown is not None and shown.result_title:
            m = RESULT_TITLE_RE.fullmatch(shown.result_title)
            count = m.group(2).strip()[1:-1] if m else ""  # " (14 rows)" -> "14 rows"
        out.append(("class:result.count.focused" if focused else "class:result.count",
                    f" {count or 'Result'} "))
        if focused and self.results.grid and self.results.grid.rows:
            out.append(("class:result.note", f"· row {self.results.row + 1}/{self.results.grid.rows} "))
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
            text = hint or self.message
            if not hint and not self.error and text.endswith(" connected"):  # "<name> connected"
                left += [sep, ("class:top", text[:-len("connected")]), ("class:top.connected", "connected")]
            else:
                left += [sep, ("class:top.error" if self.error and not hint else "class:top", text)]
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
        right = " Ctrl-P commands · Ctrl-O run · Ctrl-N new cell · Ctrl-0~2 / Ctrl-W 0~3 conn · F4 statement · Ctrl-C cancel · ↑↓ cells · Alt-1~7 panel tab · Alt-8 prompt · Alt-9 panel · Alt-0 result · Ctrl-W hjkl move · F1 save+quit · Ctrl-Q quit "
        used = sum(get_cwidth(f[1]) for f in left)
        room = get_app().output.get_size().columns - used - get_cwidth(right)
        return left + ([("", " " * room), ("class:top.dim", right)] if room > 0 else [])

    def _vi_mode(self) -> str:
        cell = self._focused_cell()
        editing = get_app().layout.current_window is self.results.edit_window
        if not self.vi or (cell is None and not editing):
            return ""
        buf = self.results.edit_buffer if cell is None else cell.buffer
        if buf.selection_state:
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
            @kb.add(ALT_DIGITS[i])  # Alt-1..7: open that panel tab
            def _(event, i=i):
                self.show_panel = True
                self.panel.select(i)
                editing = self._tpl_edit is not None and self.panel.tab.name == "archive"
                event.app.layout.focus(self.editor_window if editing else self.panel.window)
                if self.panel.tab.name == "ai" and self._ask is None and not self._session.chat.busy \
                        and not self._ai_picking():
                    self._ai_prompt()  # the AI tab: straight to the question line

        # PageUp / PageDown scroll the pane that has the focus: the shell (its cells), the
        # panel or the result pane
        @kb.add("pageup")
        def _(event):
            self.page_focused(-1)

        @kb.add("pagedown")
        def _(event):
            self.page_focused(1)

        # Ctrl+PageUp / Ctrl+PageDown always scroll the result pane
        @kb.add("c-pageup")
        def _(event):
            self.results.page(-1)

        @kb.add("c-pagedown")
        def _(event):
            self.results.page(1)

        @kb.add(MACRO_PAGE_KEY)
        @kb.add("µ", filter=vi_navigation_mode)  # what Option+M types on a Mac whose Option is not Meta
        def _(event):  # Alt+M: the macros
            self.go_to_archive("macro")

        @kb.add("c-t")
        def _(event):  # a new page of the shell
            self.new_page()

        @kb.add(PAGE_PREV)
        def _(event):
            self.step_page(-1)

        @kb.add(PAGE_NEXT)
        def _(event):
            self.step_page(1)

        @kb.add(SHIFT_SPACE)
        def _(event):  # no binding of its own: handled exactly as a plain Space
            event.app.key_processor.feed(KeyPress(" ", " "), first=True)

        @kb.add("f7")
        def _(event):
            self.toggle_autocommit()

        @kb.add("f8")
        def _(event):
            self.toggle_autotrace()

        @kb.add(RESULTS_KEY)
        def _(event):  # Alt+0: the result pane
            event.app.layout.focus(self.results.window)

        @kb.add(PANEL_KEY)
        def _(event):  # Alt+9: the side panel, on the tab it shows (the bookmark editor if open)
            self.show_panel = True
            editing = self._tpl_edit is not None and self.panel.tab.name == "archive"
            event.app.layout.focus(self.editor_window if editing else self.panel.window)

        @kb.add(PROMPT_KEY)
        def _(event):  # Alt+8: back to the prompt (the input cell) from any pane or earlier cell
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
            if self._cycle_macro(buf):  # `(` / `(*c` at the cell's start: the macros, one by one
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
                # nothing for Tab to complete (a line's start, after a blank …): a tab character
                if next(iter(self.completer.get_completions(doc, CompleteEvent(completion_requested=True))), None) is None:
                    buf.insert_text("\t")
                    return
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
        # distinctly), Alt+Enter, F5 and, in emacs mode, Ctrl-J (some terminals' Ctrl+Enter)
        # do the same. In vim mode Ctrl-J / Ctrl-K go between the cells instead (below).
        vim = Condition(lambda: self.vi)

        @kb.add("c-o", filter=in_shell)
        @kb.add(RUN_KEY, filter=in_shell)
        @kb.add(ALT_ENTER, filter=in_shell)
        @kb.add("f5", filter=in_shell)
        @kb.add("c-j", filter=in_shell & ~vim)
        def _(event):  # sync on purpose: the text must be taken before more keys arrive
            self.execute_cell(self._focused_cell())

        # vim mode, NORMAL or INSERT: Ctrl-J / Ctrl-K, the next / previous cell at once, wherever
        # the cursor is in this one (its bind lines are passed by: they are this cell's)
        in_cell_or_binds = Condition(lambda: self._cell_in_focus() is not None)

        @kb.add("c-j", filter=vim & in_cell_or_binds)
        def _(event):
            self._jump_cell(1)

        @kb.add("c-k", filter=vim & in_cell_or_binds)
        def _(event):
            self._jump_cell(-1)

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

        # Ctrl-D in the shell (a cell or its bind lines, any mode): that cell goes (it used to
        # delete the character under the cursor, and quit on an empty input: Ctrl-Q quits)
        in_cell_any = Condition(lambda: self._cell_in_focus() is not None)

        @kb.add("c-d", filter=in_cell_any)
        def _(event):
            self.delete_cell(self._cell_in_focus())

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

        # NORMAL `gg` / `G` go past the cell: the top cell's first line, the bottom cell's last.
        # With a count (`5G`, `3gg`) they go to that line of this cell, as in vim; `dG`, `ygg`
        # and VISUAL `G` keep their meaning (an operator pending is not NORMAL mode here).
        # Alt+S, and the `ß` Option+S types on a Mac whose Option is not Meta
        @kb.add("ß", filter=in_shell & vi_navigation_mode)
        def _(event):  # one cell per statement
            self.split_cell(self._focused_cell())

        @kb.add(SPLIT_KEY)
        def _(event):  # bound everywhere, so INSERT mode or another input does not type its ESC s
            if self._focused_cell() is not None and event.app.vi_state.input_mode == InputMode.NAVIGATION \
                    and self.vi:
                self.split_cell(self._focused_cell())

        @kb.add("g", "t", filter=in_shell & vi_navigation_mode)
        def _(event):  # vim's tab keys: the next page (`3gt`: page 3), `gT` the previous
            if event.arg_present:
                self.show_page(event.arg - 1)
            else:
                self.step_page(1)

        @kb.add("g", "T", filter=in_shell & vi_navigation_mode)
        def _(event):
            self.step_page(-1)

        @kb.add("g", "g", filter=in_shell & vi_navigation_mode)
        def _(event):
            if event.arg_present:
                self._goto_line(event.current_buffer, event.arg - 1)
                return
            cells = self._editable(self._session)
            if cells:
                self._focus_cell(cells[0], at_end=False)

        @kb.add("G", filter=in_shell & vi_navigation_mode)
        def _(event):
            if event.arg_present:
                self._goto_line(event.current_buffer, event.arg - 1)
                return
            cell = self._session.input_cell
            self.app.layout.focus(cell.window)
            self._goto_line(cell.buffer, cell.buffer.document.line_count - 1)  # its first non-blank, as vim's G

        @kb.add("c-n", filter=in_shell)
        def _(event):  # a new cell below the one under the cursor
            buf = event.current_buffer
            if buf.complete_state:  # while the completion menu is open, Ctrl-N still moves in it
                buf.complete_next()
                return
            self.new_cell_below(self._focused_cell())

        @kb.add("O", filter=in_shell & vi_navigation_mode)
        def _(event):  # NORMAL Shift+O: run only the line under the cursor (vim's "open line above" gives way)
            self.run_line(self._focused_cell())

        @kb.add("O", filter=in_shell & vi_selection_mode)
        def _(event):  # VISUAL (Shift+V …) Shift+O: run what is selected, not the cursor's line
            self.run_selection(self._focused_cell())

        @kb.add("P", filter=in_shell & vi_navigation_mode)
        def _(event):  # NORMAL Shift+P: a copy of the cell right below it
            self.duplicate_cell(self._focused_cell())

        @kb.add("c-r", filter=in_shell)
        def _(event):  # bookmark the cell's SQL (asks for a name and an optional @alias)
            self._template_add_cell(self._focused_cell())

        @kb.add("f4", filter=in_shell)
        def _(event):  # the complete statement under the cursor, on a connection of its own
            self.run_parallel(self._focused_cell())

        @kb.add(SCRIPT_KEY)
        def _(event):  # Ctrl+Shift+O: every statement of the cell, one after another (elsewhere: nothing)
            if self._focused_cell() is not None:
                self.run_script(self._focused_cell())

        @kb.add("f1")
        def _(event):  # snapshot the session (cells, prompt and cursor, fetched rows) and quit
            self.snapshot_and_quit()

        return kb

    @staticmethod
    def _goto_line(buf: Buffer, row: int) -> None:
        """Line `row` of the cell (the last one past its end), at its first non-blank."""
        doc = buf.document
        row = max(0, min(doc.line_count - 1, row))
        line = doc.lines[row]
        buf.cursor_position = doc.translate_row_col_to_index(row, 0) + len(line) - len(line.lstrip())

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

        # Redo: Ctrl-R in Vim, but here Ctrl-R bookmarks the cell, so NORMAL `U` redoes
        # (prompt_toolkit has no redo key of its own in vi mode).
        @kb.add("U", filter=in_shell & vi_navigation_mode, save_before=lambda e: False)
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
        self.mark_run(cell, 0, len(text))
        self.app.create_background_task(self._run_and_advance(s, cell, text))

    async def _run_and_advance(self, s: Session, cell: Cell, text: str) -> None:
        """Ctrl-O: run the cell; once it ran without an error, the focus goes on to the next
        cell, if there is one and the focus is still on this one. The result pane keeps this
        run's result until the next cell runs or the focus moves again (`_pane_hold`)."""
        version = cell.version
        await self.run_cell(s, cell, text)
        if cell.version == version or any(st == "class:error" for st, _ in cell.output):
            return  # not run (its binds dropped), or an error: stay to fix it
        if self._focused_cell() is not cell:
            return  # moved on meanwhile
        cells = [c for c in cell.page.cells if c.buffer is not None]
        i = cells.index(cell) if cell in cells else -1
        if 0 <= i < len(cells) - 1:
            after = cells[i + 1]
            self._pane_hold = (after, cell)
            self._focus_cell(after, at_end=True)
            self.app.invalidate()

    @staticmethod
    def mark_run(cell: Cell, start: int, end: int) -> None:
        """Light up the part of the cell's text that runs (RanHighlight), until it has blinked out."""
        text = cell.buffer.text
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        cell.flash = {"start": start, "end": end, "text": text, "started": time.monotonic(), "done": None}
        cell.cursor_hidden = True  # back with the next key, or when the mark goes

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
        s.history = history_view(s.runs, self.history_max)
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
        m = re.search(r"([^\W\d][\w$#]*)\.[\w$#]*$", doc.text_before_cursor)
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
        self.mark_run(source, stmt.start, stmt.end)
        self.app.create_background_task(self._run_statement(s, source, stmt.text, buf.text))

    def run_script(self, source: Cell) -> None:
        """Ctrl+Shift+O: every statement of the cell, in order, in place, like F4 one after
        another. The first error stops it there, the cursor at the statement that failed."""
        s, buf = source.session, source.buffer
        if buf.complete_state:
            buf.cancel_completion()
        if source.running:
            self.say("the cell is still running", error=True)
            return
        stmts = [st for st in script_statements(buf.text) if prepare_statement(st.text, s.profile.type)]
        if not stmts:
            self.say("Ctrl+Shift+O: no statement in the cell", error=True)
            return
        self.app.create_background_task(self._run_script(s, source, stmts, buf.text))

    @staticmethod
    def _script_head(sql: str, width: int = 48) -> str:
        """A statement on one line for the script's list: its comments left out, cut to `width`."""
        lines = [l.strip() for l in sql.splitlines() if l.strip() and not l.strip().startswith("--")]
        text = " ".join(" ".join(lines).split())
        return text if len(text) <= width else text[:width - 1] + "…"

    @staticmethod
    def _script_outcome(cell: Cell) -> tuple[bool, str]:
        """How the statement the cell just ran went: ok, and its outcome on one line."""
        bad = any(st == "class:error" for st, _ in cell.output)
        text = "".join(t for st, t in cell.output if st != "class:note.time")
        lines = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("-- ")]
        return not bad, lines[0] if lines else ""

    async def _run_script(self, s: Session, cell: Cell, stmts: list, whole: str) -> None:
        rows: list[tuple[str, str, str]] = []  # (✓ ✗ ·, the statement on one line, its outcome)
        failed = None  # (the statement that failed, its output)
        for i, st in enumerate(stmts):
            if cell.buffer.text != whole:  # edited meanwhile: the statements' places are gone
                break
            self.say(f"script: statement {i + 1}/{len(stmts)} running…")
            version = cell.version
            self.mark_run(cell, st.start, st.end)
            await self.run_cell(s, cell, st.text)
            if cell.version == version:  # it did not run (its bind values were not given)
                break
            ok, outcome = self._script_outcome(cell)
            rows.append(("✓" if ok else "✗", self._script_head(st.text), outcome))
            if not ok:
                failed = (st, list(cell.output))
                break
        done = len(rows)
        rows += [("·", self._script_head(st.text), "not run") for st in stmts[done:]]
        ok_count = sum(1 for mark, _, _ in rows if mark == "✓")
        head = f"script: {ok_count} of {len(stmts)} statement{'s' if len(stmts) != 1 else ''} ran"
        if failed:
            head += f", stopped at #{done}"
        elif done < len(stmts):
            head += ", stopped"
        out = [("class:note", head + "\n")]
        for n, (mark, sql, outcome) in enumerate(rows, 1):
            style = "class:error" if mark == "✗" else "class:note.time" if mark == "·" else "class:note"
            out.append((style, f"{mark} {n:>2}. {sql}  {outcome}\n"))
        if failed:  # the error in full, as a run of that statement alone shows it
            out += [("", "\n")] + [(st, t) for st, t in failed[1] if st == "class:error"]
        if out and out[-1][1].endswith("\n"):
            out[-1] = (out[-1][0], out[-1][1].rstrip("\n"))
        cell.output = out
        if cell.buffer.text == whole:
            cell.ran_text = whole  # what ran came from this text: the cell is not stale
        cell.version += 1
        if failed:
            # stop there: the cell focused, its cursor at the start of the statement that failed
            if cell.page in s.pages:  # not closed meanwhile
                if cell.page is not s.page:  # moved on to another page: back to this one
                    self.show_page(s.pages.index(cell.page))
                self.app.layout.focus(cell.window)
                cell.buffer.cursor_position = failed[0].start
                if self.vi:
                    self.app.vi_state.input_mode = InputMode.NAVIGATION
            self.say(f"script stopped at statement {done} of {len(stmts)}: {rows[done - 1][2]}", error=True)
        else:
            self.say(head)
        self.app.invalidate()

    def run_selection(self, source: Cell) -> None:
        """VISUAL `O` (Shift+O after Shift+V, v or Ctrl-V): what the selection covers, in place:
        one statement runs as F4 runs it; several run one after another, as Ctrl+Shift+O runs a
        cell, stopping at the first error with the cursor on it. The selection then ends (NORMAL)."""
        if source is None:
            return
        s, buf = source.session, source.buffer
        if source.running:
            self.say("the cell is still running", error=True)
            return
        ranges = list(buf.document.selection_ranges())
        buf.exit_selection()
        if self.vi:
            self.app.vi_state.input_mode = InputMode.NAVIGATION
        if not ranges:
            return
        start, end = ranges[0][0], ranges[-1][1]
        text = buf.text[start:end] if len(ranges) == 1 else "\n".join(buf.text[a:b] for a, b in ranges)
        stmts = [Statement(st.start + start, st.end + start, st.text) for st in script_statements(text)] \
            if len(ranges) == 1 else script_statements(text)
        stmts = [st for st in stmts if prepare_statement(st.text, s.profile.type)]
        if not stmts:
            self.say("O: nothing to run in the selection", error=True)
            return
        if len(stmts) == 1:
            self.mark_run(source, stmts[0].start if len(ranges) == 1 else start,
                          stmts[0].end if len(ranges) == 1 else end)
            self.app.create_background_task(self._run_statement(s, source, stmts[0].text, buf.text))
        else:
            self.app.create_background_task(self._run_script(s, source, stmts, buf.text))

    def run_line(self, source: Cell) -> None:
        """NORMAL `O` (Shift+O): only the line under the cursor, in place, like F4 for a
        statement: `select …;` on a line of its own runs alone, without the lines around it."""
        s, buf = source.session, source.buffer
        if source.running:
            self.say("the cell is still running", error=True)
            return
        line = buf.document.current_line.strip()
        if not line:
            self.say("O: the line under the cursor is empty", error=True)
            return
        if not prepare_statement(line, s.profile.type):
            return
        doc = buf.document
        at = doc.translate_row_col_to_index(doc.cursor_position_row, 0)
        self.mark_run(source, at, at + len(doc.current_line))
        self.app.create_background_task(self._run_statement(s, source, line, buf.text))

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

    # -- bind variables: one line each under the cell, `0 :cust_id = 20240101` -----------------
    # A value, once given, is used by every later run (on this connection, by name, in any
    # cell); it is changed on its line. A run asks only for a variable with no value yet.
    def _cell_edited(self, cell: Cell) -> None:
        self.drop_if_empty(cell)
        self._sync_bind_names(cell)
        # lines came in at once (`p`, a paste): the shell shows them, not only the cursor's line
        lines = cell.buffer.document.line_count
        added, cell._lines = lines - getattr(cell, "_lines", lines), lines
        if added > 0 and cell.focused():
            cell.page.container.reveal_below = max(cell.page.container.reveal_below, added)

    def _sync_bind_names(self, cell: Cell, extra: list[str] = ()) -> None:
        """The cell's bind lines follow its text: one per variable it has (and `extra`, those of
        the SQL a template call made). A variable new to the cell starts with the value another
        cell used for it last (on this connection), if one did."""
        s = cell.session
        names = list(bind_names(cell.buffer.text)) if s.profile.type in ("oracle", "sqlite") else []
        seen = {n.lower() for n in names}
        names += [n for n in extra if n.lower() not in seen]
        for n in names:
            if n.lower() not in cell.bind_values and n.lower() in s.binds:
                cell.bind_values[n.lower()] = s.binds[n.lower()]
        if tuple(names) != cell.bind_names:
            cell.bind_names = tuple(names)
            self._fill_binds(cell)

    def _fill_binds(self, cell: Cell) -> None:
        """The cell's bind lines as its values are (a variable with none: empty)."""
        text = "\n".join(cell.bind_values.get(n.lower(), "") for n in cell.bind_names)
        if cell.bind_buffer.text != text:
            at = min(cell.bind_buffer.cursor_position, len(text))
            cell._bind_filling = True
            try:
                cell.bind_buffer.set_document(Document(text, at), bypass_readonly=True)
            finally:
                cell._bind_filling = False

    def _bind_edited(self, cell: Cell) -> None:
        """A value changed on the cell's bind lines: the cell's own from now on (another cell
        keeps its value; a cell that gets the variable later starts with this one); a line left
        empty for a new variable stays unset."""
        if getattr(cell, "_bind_filling", False):
            return
        for name, value in zip(cell.bind_names, cell.bind_buffer.text.split("\n")):
            if value.strip() or name.lower() in cell.bind_values:
                self._set_bind(cell, name, value.strip())

    @staticmethod
    def _set_bind(cell: Cell, name: str, value: str) -> None:
        cell.bind_values[name.lower()] = value
        cell.session.binds[name.lower()] = value  # the latest, for a cell that gets it later

    def _bind_prefix(self, cell: Cell, n: int):
        if n >= len(cell.bind_names):
            return [("class:list.note", "    (no variable) ")]
        name = cell.bind_names[n]
        if self.binds_active(cell):  # a value is being typed: the name pink and bold
            return [("", "  "), ("class:bind.name", f":{name}"), ("class:list.note", " = ")]
        return [("class:bind.idle", f"  :{name} = ")]  # ran, or another cell has the focus: gray

    def binds_active(self, cell: Cell) -> bool:
        """A value is being typed on the cell's bind lines: the cursor is on them, or a run
        waits there for one."""
        return ((self._bind_wait is not None and self._bind_wait["cell"] is cell)
                or (hasattr(self, "app") and self.app.layout.current_window is cell.bind_window))

    def binds_shown(self, cell: Cell) -> bool:
        """The cell's bind lines show once it ran with them, or while a run waits for a value."""
        return bool(cell.bind_names) and (cell.binds_ran or (self._bind_wait is not None
                                                             and self._bind_wait["cell"] is cell))

    def _bind_keys(self) -> KeyBindings:
        kb = KeyBindings()
        cell = lambda: self.bind_of.get(get_app().layout.current_window)
        row = lambda: get_app().current_buffer.document.cursor_position_row
        first = Condition(lambda: row() == 0)
        last = Condition(lambda: row() == get_app().current_buffer.document.line_count - 1)
        kb.add("enter")(lambda e: self._bind_enter(cell()))  # any mode: run with these values
        kb.add("c-o")(lambda e: self._bind_enter(cell()))
        kb.add("c-c")(lambda e: self._bind_cancel(cell()))
        kb.add("c-n")(lambda e: cell() and self.new_cell_below(cell()))  # as in the cell: a new one below
        # off the first line: back to the cell's SQL; off the last: on to the next cell
        kb.add("k", filter=first & vi_navigation_mode)(lambda e: self._focus_cell(cell(), at_end=True))
        kb.add("up", filter=first)(lambda e: self._focus_cell(cell(), at_end=True))
        kb.add("j", filter=last & vi_navigation_mode)(lambda e: self._step(cell(), 1, from_binds=True))
        kb.add("down", filter=last)(lambda e: self._step(cell(), 1, from_binds=True))
        return kb

    def _bind_enter(self, cell: Cell | None) -> None:
        if cell is None:
            return
        w = self._bind_wait
        if w is not None and w["cell"] is cell:
            # several values asked for: Enter goes on to the next one still empty, below; the run
            # goes on from the last one
            buf, lowered = cell.bind_buffer, [n.lower() for n in cell.bind_names]
            lines, row = buf.text.split("\n"), buf.document.cursor_position_row
            waiting = {n.lower() for n in w["missing"]}
            later = [i for i in range(row + 1, len(lowered))
                     if lowered[i] in waiting and not (lines[i].strip() if i < len(lines) else "")]
            if later:
                i = later[0]
                buf.cursor_position = buf.document.translate_row_col_to_index(i, 0) + len(lines[i])
                self.say(f"bind: a value for :{cell.bind_names[i]} · Enter goes on · Ctrl-C drops")
                return
            for name in cell.bind_names:
                if name.lower() not in cell.bind_values:  # left empty: '' (NULL in Oracle)
                    self._set_bind(cell, name, "")
            w["future"].done() or w["future"].set_result(True)
        elif not cell.running:  # a value changed: run the cell again with it
            self.app.layout.focus(cell.window)
            self.execute_cell(cell)

    def _bind_cancel(self, cell: Cell | None) -> None:
        w = self._bind_wait
        if cell is not None and w is not None and w["cell"] is cell:
            w["future"].done() or w["future"].set_result(False)

    async def _ask_binds(self, s: Session, cell: Cell, names: list[str]) -> dict[str, str | None] | None:
        """The values of the statement's bind variables: those the session has (given before,
        changed on the cell's bind lines), and for a variable with none yet, typed on its line
        under the cell while the run waits (Enter goes on, Ctrl-C drops the run: None).
        Values bind as strings; `NULL` binds NULL."""
        self._sync_bind_names(cell, extra=names)
        if any(n.lower() not in cell.bind_values for n in names):
            async with self._bind_lock:  # one run at a time waits for values (F4 runs several)
                missing = [n for n in names if n.lower() not in cell.bind_values]
                if missing:
                    ok = await self._wait_for_binds(s, cell, missing)
                    if not ok:
                        return None
        self.say("")
        values = {n.lower(): cell.bind_values.get(n.lower(), "") for n in names}
        return {k: (None if v.upper() == "NULL" else v) for k, v in values.items()}

    async def _wait_for_binds(self, s: Session, cell: Cell, missing: list[str]) -> bool:
        if cell.page in s.pages and cell.page is not s.page:
            self.show_page(s.pages.index(cell.page))
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._bind_wait = {"cell": cell, "future": future, "missing": list(missing)}
        lowered = [n.lower() for n in cell.bind_names]
        row = lowered.index(missing[0].lower()) if missing[0].lower() in lowered else 0
        doc = cell.bind_buffer.document
        self.app.layout.focus(cell.bind_window)
        cell.bind_buffer.cursor_position = doc.translate_row_col_to_index(row, 0) + len(doc.lines[row])
        if self.vi:
            self.app.vi_state.input_mode = InputMode.INSERT
        more = f" (and {len(missing) - 1} more)" if len(missing) > 1 else ""
        self.say(f"bind: a value for :{missing[0]}{more} on its line under the cell · Enter runs · Ctrl-C drops")
        self.app.invalidate()
        try:
            return await future
        finally:
            self._bind_wait = None
            if self.app.layout.current_window is cell.bind_window:
                self.app.layout.focus(cell.window)
                if self.vi:
                    self.app.vi_state.input_mode = InputMode.NAVIGATION
            self.app.invalidate()

    async def run_cell(self, s: Session, cell: Cell, text: str) -> None:
        """Run a cell's text and store the outcome on the cell (in place, also for re-runs)."""
        if self._pane_hold is not None and self._pane_hold[0] is cell:
            self._pane_hold = None  # the cell Ctrl-O went on to runs: its own result shows
        cell.running = True
        cell.affected = None
        self._drop_pager(cell)  # a re-run: the earlier result's cursor goes
        self.app.invalidate()
        pre: list[tuple[str, str]] = []
        given: dict[str, str] = {}  # `:1`, `:2` … of a template or macro: its arguments
        macro = False
        try:
            try:
                called = self.templates.expand_macro(text)
            except TemplateError as e:
                self._finish(s, cell, text, [("class:error", f"ERROR: {e}")])
                return
            if called is not None:  # `(name args)`: its query runs, the cell shows the call
                (sql, given), macro = called, True
            elif "\n" not in text.strip() and Templates.is_call(text):
                call = text.strip()
                if call == "@":
                    self._remember(s, text)
                    self._finish(s, cell, text, [("class:note", self.templates.listing())])
                    return
                try:
                    sql, given = self.templates.expand_call(call)
                except TemplateError as e:
                    self._finish(s, cell, text, [("class:error", f"ERROR: {e}")])
                    return
                pre = [("class:note", "".join(f"-- {l}\n" for l in sql.splitlines()))]
            else:
                sql = prepare_statement(text, s.profile.type)
            binds = dict(given) or None
            names = bind_names(sql) if sql and s.profile.type in ("oracle", "sqlite") else []
            named = [n for n in names if n.lower() not in given]  # the others: the cell's bind lines
            if named:
                asked = await self._ask_binds(s, cell, named)
                if asked is None:
                    self.say("not run: no values for its bind variables")
                    return
                binds = {**given, **asked}
                cell.binds_ran = True  # its bind lines show from now on, with the values bound
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
                            None, db.execute_traced, sql, binds)
                    else:
                        res = await asyncio.get_running_loop().run_in_executor(None, db.execute, sql, binds)
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
                cell.ran_sql = sql
                first = " ".join((text if macro else sql).split()).rstrip(";")  # a macro: its call
                title = f"{first[:60]}{'…' if len(first) > 60 else ''} {footer}"
                self._finish(s, cell, text, pre + [("class:note", footer), ("class:note.time", f"  {stamp}")], build(res.columns, res.rows, res.kinds, res.types), title,
                             trace, (res.columns, res.rows, res.kinds, res.types))
            else:
                cell.affected = res.rowcount
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
            if cell.flash is not None and cell.flash["done"] is None:
                cell.flash["done"] = time.monotonic()  # the hold, then the blinks, start now
            self.app.invalidate()

    def _finish(self, s: Session, cell: Cell, text: str, output, grid: Grid | None = None,
                title: str = "", trace: list[str] | None = None, data=([], [], None)) -> None:
        output = red_cross(output)
        cell.output, cell.grid, cell.result_title, cell.trace = output, grid, title, trace
        cell.data = data
        cell.ran_text = text
        cell.version += 1
        if cell.keep_pane:  # the result it changed stays the one shown (and the last run)
            return
        page = cell.page  # its own page, which may no longer be the one shown
        page.last_run = cell
        self._save_last(s, cell)
        focused = self._focused_cell()
        if page is not s.page or focused is None or focused.is_input or focused is cell:
            page.shown = cell  # show it now unless the user moved on to another history cell

    # -- a value edited in the result grid: an UPDATE in a cell of its own ----------
    def edit_value(self, row: int, col: int, text: str) -> None:
        s, grid = self._session, self.results.grid
        source = s.shown
        if grid is None or source is None or source.grid is not grid or not source.has_run:
            self.say("the result shown is not one to edit", error=True)
            return
        if text == copy_text(grid.value(row, col)) or (text == "NULL" and grid.value(row, col) is None):
            self.say("unchanged")
            return
        self.app.create_background_task(self._edit_value(s, source, grid, row, col, text))

    async def _edit_value(self, s: Session, source: Cell, grid: Grid, row: int, col: int, text: str) -> None:
        kind = s.profile.type
        try:
            table = source_table(source.ran_sql or source.ran_text)
        except EditError as e:
            self.say(f"cannot edit: {e}", error=True)
            return
        known = s.columns.get(table.lower())
        if known is None and s.db is not None and source.conn_index == 0:
            try:  # not in the catalog yet: ask the table for its columns
                async with s.lock:
                    known = await asyncio.get_running_loop().run_in_executor(None, s.db.columns_of, table)
            except Exception:
                known = None
        columns, rows, _cells, kinds, types, numeric = grid.source
        try:
            sql, shown = build_update(kind, table, list(columns), rows[row], kinds, types, numeric, col, text,
                                      {c.lower() for c in known} if known else None)
        except EditError as e:
            self.say(f"cannot edit: {e}", error=True)
            return
        cell = Cell(self, s)
        cell.page = source.page  # beside the result edited, whichever page shows now
        cell.conn_index, cell.keep_pane = source.conn_index, True
        cell.buffer.set_document(Document(sql, len(sql)), bypass_readonly=True)
        self._append_cell(s, cell)
        self.mark_run(cell, 0, len(sql))
        await self.run_cell(s, cell, sql)
        cell.keep_pane = False  # run again by hand later, it is a cell like any other
        if not cell.affected:
            if cell.affected == 0:
                self.say("UPDATE changed no row: the row is not as shown any more", error=True)
            return
        if grid.rows > row and grid.source[1][row] is rows[row]:
            old = grid.set_value(row, col, shown)
            data_rows = source.data[1] if source.data else None
            if isinstance(data_rows, list):  # what is saved for the next start
                for n, r in enumerate(data_rows):
                    if r is old:
                        data_rows[n] = grid.source[1][row]
                        break
        pending = "" if s.db is None or getattr(s.db, "autocommit", True) else " · not committed (commit; to keep it)"
        self.say(f"{columns[col]} updated in {cell.affected} row{'s' if cell.affected != 1 else ''}{pending}",
                 error=cell.affected != 1)
        self.app.invalidate()

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
                self.app.key_processor.before_key_press += self._show_cursor
                self.app.key_processor.before_key_press += self._count_key
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
        templates: Templates | None = None, ai: AISettings | None = None) -> None:
    App(profiles, abbrevs, ui or UISettings(), templates or Templates(), ai).run()
