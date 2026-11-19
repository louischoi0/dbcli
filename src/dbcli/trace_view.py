"""The trace tab's drawing: autotrace's text turned into colored, tree-shaped lines.

`render(lines)` takes what `trace.format_trace` made and returns one list of
(style, text) fragments per screen line. A DBMS_XPLAN plan table becomes a tree:

    ▌ Actual Plan · DISPLAY_CURSOR
       0  SELECT STATEMENT            actual 3 · buffers 7
       1* └─ TABLE ACCESS FULL  T  estimated 3 · actual 3 · buffers 7 · 0.01s

each operation in its own color (full scans, nested loops, hash and merge joins,
index lookups, rowid lookups, sorts), predicates under the tree, statistics with
their figures stood out.
"""
from __future__ import annotations

import re

from prompt_toolkit.utils import get_cwidth

from .trace import RULE

Fragments = list[tuple[str, str]]

# operation -> style; the first pattern that matches wins
OPERATION_STYLES: tuple[tuple[re.Pattern, str], ...] = tuple((re.compile(p), s) for p, s in (
    (r"^TABLE ACCESS (STORAGE )?FULL", "class:op.full"),
    (r"^(INDEX (FAST )?FULL SCAN|INDEX SKIP SCAN)", "class:op.indexfull"),
    (r"^TABLE ACCESS BY (USER |GLOBAL |LOCAL )?(INDEX )?ROWID", "class:op.rowid"),  # key lookup
    (r"^INDEX (RANGE|UNIQUE) SCAN", "class:op.index"),  # index lookup
    (r"^NESTED LOOPS", "class:op.nl"),
    (r"^HASH JOIN", "class:op.hash"),
    (r"^(MERGE JOIN|SORT JOIN)", "class:op.merge"),
    (r"^(SORT|HASH GROUP BY|HASH UNIQUE|WINDOW|BUFFER SORT)", "class:op.sort"),
    (r"^(SELECT|INSERT|UPDATE|DELETE|MERGE) STATEMENT", "class:op.statement"),
    (r"^(PX |PARTITION )", "class:op.parallel"),
))
KEY_STATS = {"consistent gets", "physical reads", "db block gets", "redo size"}
IVORY_STATS = {"recursive calls", "consistent gets", "rows processed"}  # the name bold ivory, the value bold orange
STAT_RE = re.compile(r"^\s*(\d+)\s{2}(.+)$")
PREDICATE_RE = re.compile(r"^(\s*)(\d+)\s-\s(access|filter)(.*)$")
SECTION_TITLES = {"Execution Plan": "Execution Plan · estimated", "Statistics": "Statistics"}


def operation_style(operation: str) -> str:
    for pattern, style in OPERATION_STYLES:
        if pattern.match(operation):
            return style
    return "class:op"


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _parse_table(block: list[str]) -> tuple[list[str], list[dict]] | None:
    """A DBMS_XPLAN table's header and its rows (`depth` from the Operation column's indent)."""
    rows = [l for l in block if l.startswith("|")]
    if len(rows) < 2:
        return None
    header = _cells(rows[0])
    if "Operation" not in header:
        return None
    op_at = header.index("Operation")
    out = []
    for line in rows[1:]:
        raw = line.strip().strip("|").split("|")
        if len(raw) != len(header):
            continue
        cells = {h: c.strip() for h, c in zip(header, raw)}
        operation = raw[op_at]
        cells["depth"] = len(operation) - len(operation.lstrip()) - 1
        cells["Operation"] = operation.strip()
        ident = cells.get("Id", "")
        cells["star"] = ident.startswith("*")
        cells["Id"] = ident.lstrip("*").strip()
        out.append(cells)
    return header, out


def _tree_prefixes(depths: list[int]) -> list[str]:
    """`├─ ` / `└─ ` connectors with `│  ` rails, from each row's depth."""
    prefixes = []
    for i, d in enumerate(depths):
        if d <= 0:
            prefixes.append("")
            continue
        parts = []
        for level in range(1, d + 1):
            # is there a later row at this level before the branch closes?
            more = False
            for later in depths[i + 1:]:
                if later < level:
                    break
                if later == level:
                    more = True
                    break
            if level == d:
                parts.append("├─ " if more else "└─ ")
            else:
                parts.append("│  " if more else "   ")
        prefixes.append("".join(parts))
    return prefixes


def _seconds(a_time: str) -> str:
    m = re.fullmatch(r"(\d+):(\d+):(\d+(?:\.\d+)?)", a_time)
    if not m:
        return a_time
    total = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return f"{total:.2f}s"


def _metrics(row: dict) -> Fragments:
    """The figures beside an operation: estimates, or actuals when the plan has them."""
    parts: Fragments = []

    def add(label: str, value: str, style: str = "class:trace.value", label_style: str = "class:trace.dim") -> None:
        if value:
            if parts:
                parts.append(("class:trace.dim", " · "))
            parts.append((label_style, f"{label} " if label else ""))
            parts.append((style, value))

    if "A-Rows" in row:
        e, a = row.get("E-Rows", ""), row.get("A-Rows", "")
        off = _far_off(e, a)
        add("estimated", e)
        add("actual", a, "class:trace.warn" if off else "class:trace.strong", label_style="class:trace.rowlabel")
        add("buffers", row.get("Buffers", ""))
        add("", _seconds(row.get("A-Time", "")))
    else:
        add("rows", row.get("Rows", ""))
        add("cost", row.get("Cost (%CPU)", "").split(" ")[0])
    return parts


def _number(text: str) -> float | None:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([KMG]?)", text.strip())
    if not m:
        return None
    return float(m.group(1)) * {"": 1, "K": 1e3, "M": 1e6, "G": 1e9}[m.group(2)]


def _far_off(estimate: str, actual: str) -> bool:
    """The optimizer guessed rows at least 10x off: worth a look."""
    e, a = _number(estimate), _number(actual)
    if e is None or a is None:
        return False
    lo, hi = sorted((max(e, 1), max(a, 1)))
    return hi / lo >= 10


def _plan(block: list[str]) -> list[Fragments]:
    parsed = _parse_table(block)
    if parsed is None:
        return [[("class:trace.dim", f"  {l}")] for l in block if l.strip()]
    _, rows = parsed
    out: list[Fragments] = []
    for line in block:  # what DBMS_XPLAN says above its table
        if line.startswith(("SQL_ID", "Plan hash value")):
            out.append([("class:trace.dim", f"  {line.strip()}")])
    prefixes = _tree_prefixes([r["depth"] for r in rows])
    op_width = max((get_cwidth(p) + get_cwidth(r["Operation"]) for p, r in zip(prefixes, rows)), default=0)
    name_width = max((get_cwidth(r.get("Name", "")) for r in rows), default=0)
    for prefix, row in zip(prefixes, rows):
        style = operation_style(row["Operation"])
        pad = " " * (op_width - get_cwidth(prefix) - get_cwidth(row["Operation"]))
        name = row.get("Name", "")
        line: Fragments = [
            ("class:trace.id", f"{row['Id']:>3}"),
            ("class:trace.star" if row["star"] else "", "*" if row["star"] else " "),
            ("", " "), ("class:trace.rail", prefix), (style, row["Operation"]), ("", pad),
            ("", "  "), ("class:trace.name", name.ljust(name_width)), ("", "  "),
        ]
        out.append(line + _metrics(row))
    # predicates and notes, as DBMS_XPLAN prints them below the table
    after = block[max(i for i, l in enumerate(block) if l.startswith("|")) + 1:]
    in_note, has_predicates = False, False
    for line in after:
        text = line.rstrip()
        bare = text.strip()
        if not bare or set(bare) <= {"-"} or bare.startswith("Predicate Information"):
            continue
        m = PREDICATE_RE.match(text)
        if bare == "Note":
            in_note = True
            out += [[], [("class:trace.subtitle", "  Note")]]
        elif m and not in_note:
            if not has_predicates:
                out += [[], [("class:trace.subtitle", "  Predicates")]]
                has_predicates = True
            kind = m.group(3)
            out.append([("class:trace.id", f"{m.group(2):>3}"), ("", "  "),
                        ("class:trace.access" if kind == "access" else "class:trace.filter", kind),
                        ("class:trace.value", m.group(4))])
        else:  # a note, or a predicate carried onto the next line
            out.append([("class:trace.dim" if in_note else "class:trace.value", f"  {bare}")])
    return out


def _stats(block: list[str]) -> list[Fragments]:
    out: list[Fragments] = []
    for line in block:
        m = STAT_RE.match(line)
        if not m:
            if line.strip():
                out.append([("class:trace.warn" if line.startswith("(") else "class:trace.dim",
                             f"  {line.strip()}")])
            continue
        value, name = m.group(1), m.group(2)
        if name in IVORY_STATS:  # the ones read first: always bold ivory, zeros too
            out.append([("class:trace.ivory.value", f"{int(value):>11,}"), ("", "  "), ("class:trace.ivory", name)])
            continue
        if name == "rows processed":
            style = "class:trace.strong"
        elif value == "0":
            style = "class:trace.dim"
        else:
            style = "class:trace.key" if name in KEY_STATS else "class:trace.value"
        out.append([(style, f"{int(value):>11,}"), ("", "  "),
                    ("class:trace.statname" if value != "0" else "class:trace.dim", name)])
    return out


def _sections(lines: list[str]) -> list[tuple[str, list[str]]]:
    """(title, body) per section: a title is the line before a RULE line."""
    out: list[tuple[str, list[str]]] = []
    i = 0
    while i < len(lines):
        if i + 1 < len(lines) and lines[i + 1] == RULE:
            out.append((lines[i], []))
            i += 2
            continue
        if out:
            out[-1][1].append(lines[i])
        i += 1
    return out


def render(lines: list[str]) -> list[Fragments]:
    out: list[Fragments] = []
    for title, body in _sections(lines):
        while body and not body[-1].strip():
            body = body[:-1]
        if title.startswith("Actual Plan"):
            heading = "Actual Plan · DISPLAY_CURSOR"
        else:
            heading = SECTION_TITLES.get(title, title)
        out.append([("class:trace.bar", "▌"), ("class:trace.title", f" {heading}")])
        out += _stats(body) if title == "Statistics" else _plan(body)
        out.append([])
    while out and not out[-1]:
        out.pop()
    return out

