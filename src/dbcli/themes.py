"""Color themes: one palette per theme, and the prompt_toolkit style made from it.

Every theme is for a dark terminal (dbcli draws no background of its own, only marks:
the selection, the focused cell, alternate result rows). `sky` is the default; the Settings
tab (or `[ui] theme` in config.toml) picks another.
"""
from __future__ import annotations

from dataclasses import dataclass

from prompt_toolkit.styles import Style


@dataclass(frozen=True)
class Theme:
    name: str
    title: str
    about: str
    colors: dict


# sky: soft grays, sky blue for focus and accents, orange for ids and switches that are off
SKY = {
    "accent": "#8cc8ec", "accent_deep": "#5f87af", "second": "#ff8700",
    "text": "#bcbcbc", "light": "#dadada", "dim": "#7a7a7a", "faint": "#4a4a4a",
    "input": "#d0d0d0", "ghost": "#626262",
    "select_bg": "#2d3d48", "menu_bg": "#262a2d", "cell_bg": "#1f2a31", "row_alt": "#1f2326",
    "grid_cursor": "#34495a", "palette_bg": "#262626", "scroll_bg": "#2a2a2a",
    "error": "#d78787", "stale": "#d7af87", "ok": "#a6d189", "running": "#ffd75f",
    "relation": "#ffd7af", "symbol": "#fadde9", "hint": "#ececec", "hint_arg": "#d7af87",
    "number": "#a6d189", "string": "#e5c890", "date": "#ca9ee6", "bool": "#ef9f76", "bytes": "#81c8be",
    "conn1": "#87d7af", "conn2": "#d7af87", "conn3": "#d787af",
}

THEMES = {t.name: t for t in (
    Theme("sky", "Sky", "soft grays with sky blue (the default)", SKY),
    Theme("forest", "Forest", "greens and teal, amber accents", dict(
        SKY, accent="#8fd19e", accent_deep="#4f8a5e", second="#e0b050",
        text="#bfc5bd", light="#dfe5dc", dim="#7b857a", faint="#4a524a", input="#d3d9d0",
        select_bg="#2b3d30", menu_bg="#252b26", cell_bg="#1f2b22", row_alt="#1f2420",
        grid_cursor="#35503d", palette_bg="#242824", scroll_bg="#2a2e2a",
        relation="#f2d49b", symbol="#cfe8d5", hint_arg="#e0b050",
        number="#9ccfd8", string="#f2d49b", date="#c4a7e7", bool="#eb9f7a", bytes="#7fc8b0",
        conn1="#9ccfd8", conn2="#f2d49b", conn3="#e8a0bf")),
    Theme("ember", "Ember", "warm amber and copper, teal accents", dict(
        SKY, accent="#f0b46a", accent_deep="#a06a35", second="#5fc8c0",
        text="#c8beb4", light="#e6dcd2", dim="#857a70", faint="#52483f", input="#dcd2c8",
        select_bg="#45352a", menu_bg="#2c2622", cell_bg="#2e241d", row_alt="#262120",
        grid_cursor="#57412f", palette_bg="#2a2420", scroll_bg="#302a26",
        error="#ff8a80", stale="#e6c07b", ok="#b5cc7a", running="#5fc8c0",
        relation="#ffd0a0", symbol="#f5d5c5", hint_arg="#e6c07b",
        number="#b5cc7a", string="#f0c674", date="#d4a5d9", bool="#ff9e64", bytes="#5fc8c0",
        conn1="#b5cc7a", conn2="#5fc8c0", conn3="#d4a5d9")),
    Theme("dusk", "Dusk", "lavender and violet, pink accents", dict(
        SKY, accent="#b8a4f0", accent_deep="#6e5aa8", second="#f087b4",
        text="#c0bccb", light="#e0dcea", dim="#7d788a", faint="#4b4757", input="#d6d2e0",
        select_bg="#352f4a", menu_bg="#28252f", cell_bg="#251f33", row_alt="#221f28",
        grid_cursor="#44395f", palette_bg="#26232d", scroll_bg="#2c2933",
        stale="#e8c39e", running="#f5d76e",
        relation="#f5c2e7", symbol="#d5d0f5", hint_arg="#e8c39e",
        number="#a6e3a1", string="#f9e2af", date="#89dceb", bool="#fab387", bytes="#94e2d5",
        conn1="#a6e3a1", conn2="#f9e2af", conn3="#89dceb")),
)}
DEFAULT_THEME = "sky"


def style_dict(c: dict) -> dict[str, str]:
    """Every style class dbcli uses, colored from a theme's palette."""
    accent, deep, second = c["accent"], c["accent_deep"], c["second"]
    text, light, dim, faint = c["text"], c["light"], c["dim"], c["faint"]
    pal = c["palette_bg"]
    return {
        "ghost": c["ghost"],
        # SQL in the input area: a soft white of its own (not the terminal's default), so the
        # hints can be whiter than it
        "input": c["input"],
        "keyword": f"{accent} bold",  # select, from, where …
        "relation": f"{c['relation']} bold",  # the tables and views a statement uses
        "hint": f"{c['hint']} bold",  # optimizer hints in /*+ ... */
        "hint.frame": f"{c['symbol']} bold",  # the /*+ and */ around them: as other symbols
        "symbol": f"{c['symbol']} bold",  # , ( ) + - * / | = < > …
        "hint.arg": c["hint_arg"],  # what a hint names in its parentheses: aliases, indexes, @query blocks
        "missing": "#ff6b6b underline",  # a table, view or column the database does not have
        "relation missing": "#ff6b6b bold underline",  # a missing table stays bold, as relation names are
        "prompt": f"{accent} bold",
        "prompt.dim": faint,
        "prompt.num": dim,  # `#c0`: the cell's number from the top
        "prompt.stale": f"{c['stale']} bold",
        "prompt.running": f"{c['running']} bold",
        # each connection's label on a cell, its own color
        "prompt.conn0": second, "prompt.conn1": c["conn1"], "prompt.conn2": c["conn2"], "prompt.conn3": c["conn3"],
        "cell.focused": f"bg:{c['cell_bg']}",  # an earlier cell being edited / re-run
        "note": dim,
        "note.cancel": "#ff6b6b bold",  # ✗: a run cancelled with Ctrl-C
        "note.time": f"{faint} italic",  # when a run finished, beside its row count
        "error": c["error"],
        "stale": f"{c['stale']} italic",
        "border": faint,
        "selected": f"bg:{c['select_bg']}",  # vim visual selection
        # pane titles and panel tabs: no background, a thin rule instead
        "title": dim,
        "title.focused": f"{accent} bold",
        "tab": dim,
        "tab.active": f"{light} bold",
        "tab.active.focused": f"bg:{accent} #1c1c1c bold",  # the panel has focus: its tab is filled
        # lists in the panel
        "list.active": accent,
        "list.cursor": f"bg:{c['select_bg']} {light}",
        "palette": f"bg:{pal}",  # Ctrl-P's command list
        "palette.group": f"bg:{pal} {dim}",
        "palette.key": f"bg:{pal} {second}",
        "palette.match": f"bg:{pal} {accent} bold",
        "list.note": dim,
        "list.id": second,  # a row's number in the panel: History's run numbers, Bookmark's ids
        "list.empty": f"{dim} italic",
        # the AI tab
        "ai.you": f"{accent} bold",  # a question
        "ai.text": c["input"],  # an answer's prose
        "ai.code": faint,  # the bar in front of an answer's SQL
        "ai.prompt": f"{second} bold",  # `prompt>` in front of the AI question line
        "ai.ref": f"{accent} bold",  # `@c0` in a question: that cell's SQL goes along
        # schema browser
        "schema.kind": f"{light} bold",
        "schema.title": f"{accent} bold",
        "schema.back": accent,
        "schema.invalid": c["error"],
        "schema.ddl": "",
        # top bar
        "top": text,
        "top.name": f"{light} bold",
        "top.sep": faint,
        "top.online": accent,
        "top.connected": f"{c['ok']} bold",  # the `connected` of "<name> connected"
        "top.offline": c["error"],
        "top.wait": dim,
        "top.dim": dim,
        "top.error": c["error"],
        "top.mode": f"{accent} bold",
        "top.off": f"{second} bold",  # the `off` of autocommit / autotrace
        # result table
        "grid.border": faint,
        "grid.header": f"{accent} bold",
        "grid.row": "",
        "grid.row.alt": f"bg:{c['row_alt']}",
        "grid.null": f"{dim} italic",
        # values by type
        "grid.number": c["number"],
        "grid.text": c["string"],
        "grid.date": c["date"],
        "grid.bool": c["bool"],
        "grid.bytes": c["bytes"],
        # the header's N / VCH / T …: its values' color, toned down
        "grid.type": f"nobold {dim}",
        "grid.type.number": f"nobold {mix(c['number'], 0.6)}",
        "grid.type.text": f"nobold {mix(c['string'], 0.6)}",
        "grid.type.date": f"nobold {mix(c['date'], 0.6)}",
        "grid.type.bytes": f"nobold {mix(c['bytes'], 0.6)}",
        "grid.type.bool": f"nobold {mix(c['bool'], 0.6)}",
        "grid.cursor": f"bg:{c['grid_cursor']}",  # the selected row: a background only, values keep their colors
        "grid.cell": f"bg:{deep} #ffffff bold",  # the selected cell: white on the accent, the strongest mark
        # completion menu and scrollbars in the same tones
        "completion-menu": f"bg:{c['menu_bg']} {text}",
        "completion-menu.completion.current": f"bg:{deep} #ffffff",
        "completion-menu.meta.completion": f"bg:{c['menu_bg']} {dim}",
        "completion-menu.meta.completion.current": f"bg:{deep} {light}",
        # trace tab: one color per kind of plan operation (the same in every theme: they mean things)
        "op": text,
        "op.statement": f"{light} bold",
        "op.full": "#ff7b72 bold",  # TABLE ACCESS FULL
        "op.indexfull": "#f0a35e",  # INDEX FULL / FAST FULL / SKIP SCAN
        "op.index": "#7ee787",  # INDEX RANGE / UNIQUE SCAN: index lookup
        "op.rowid": "#56d4dd",  # TABLE ACCESS BY INDEX ROWID: key lookup
        "op.nl": "#79a8ff",  # NESTED LOOPS
        "op.hash": "#e3c35a",  # HASH JOIN
        "op.merge": "#d2a8ff",  # MERGE JOIN / SORT JOIN
        "op.sort": "#ff9bce",  # SORT, GROUP BY, WINDOW
        "op.parallel": "#a5b4fc",
        "trace.bar": accent,
        "trace.title": f"{accent} bold",
        "trace.subtitle": f"{light} bold",
        "trace.id": dim,
        "trace.star": f"{c['stale']} bold",
        "trace.rail": faint,
        "trace.name": light,
        "trace.dim": dim,
        "trace.value": text,
        "trace.strong": f"{light} bold",
        "trace.key": f"{accent} bold",
        "trace.warn": f"{c['stale']} bold",
        "trace.statname": text,
        "trace.ivory": "#eee8d0 bold",  # recursive calls, consistent gets, rows processed (the names)
        "trace.ivory.value": f"{second} bold",  # … and their values
        "trace.rowlabel": f"{second} bold",  # `actual` in the actual plan
        "trace.access": "#7ee787 bold",
        "trace.filter": "#e3c35a bold",
        "scrollbar.background": f"bg:{c['scroll_bg']}",
        "scrollbar.button": f"bg:{deep}",
        "scrollbar.arrow": accent,
        # the Settings tab: a theme's swatches
        **{f"swatch.{t.name}.{k}": f"bg:{t.colors[k]}" for t in THEMES.values() for k in SWATCH},
    }


SWATCH = ("accent", "second", "number", "string", "date", "relation")  # the colors a theme line shows


def mix(color: str, keep: float, base: str = "#1c1c1c") -> str:
    """A color toned down toward the terminal's dark background (`keep` of it stays)."""
    a = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(base[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * keep + y * (1 - keep)):02x}" for x, y in zip(a, b))


BACKGROUND = "#1c1c1c"  # what a dark terminal's background is taken to be (a fade ends there)


def flash_colors(name: str) -> tuple[str, str, str]:
    """The text a cell just ran: its background when lit, and the two it fades into (the
    terminal's, and the focused earlier cell's own)."""
    c = THEMES.get(name, THEMES[DEFAULT_THEME]).colors
    return mix(c["accent"], 0.3), BACKGROUND, c["cell_bg"]


def style(name: str) -> Style:
    return Style.from_dict(style_dict(THEMES.get(name, THEMES[DEFAULT_THEME]).colors))
