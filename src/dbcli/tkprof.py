"""TKPROF's call table from a 10046 SQL trace: Parse / Execute / Fetch of one statement.

autotrace turns the trace on around the statement only, reads the new lines of the
session's trace file back (V$DIAG_TRACE_FILE_CONTENTS) and hands them here. Each call
line reads `PARSE #cur:c=cpu,e=elapsed,p=disk,cr=query,cu=current,mis=..,r=rows,dep=..`
(cpu and elapsed in microseconds); dep=0 is the statement itself, dep>0 the recursive
SQL it caused, summed into one line as TKPROF's recursive totals.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

CALLS = ("Parse", "Execute", "Fetch")
_KIND = {"PARSE": "Parse", "EXEC": "Execute", "FETCH": "Fetch"}
CALL_RE = re.compile(r"^(PARSE|EXEC|FETCH) #(\d+):c=(\d+),e=(\d+),p=(\d+),cr=(\d+),cu=(\d+),"
                     r"mis=\d+,r=(\d+),dep=(\d+)")
CURSOR_RE = re.compile(r"^PARSING IN CURSOR #(\d+) len=\d+ dep=(\d+)")
COLUMNS = ("count", "cpu", "elapsed", "disk", "query", "current", "rows")


@dataclass
class Row:
    count: int = 0
    cpu: int = 0  # microseconds
    elapsed: int = 0  # microseconds
    disk: int = 0
    query: int = 0
    current: int = 0
    rows: int = 0

    def add(self, other: "Row") -> None:
        for name in COLUMNS:
            setattr(self, name, getattr(self, name) + getattr(other, name))


@dataclass
class CallStats:
    calls: dict[str, Row] = field(default_factory=lambda: {c: Row() for c in CALLS})
    recursive: Row = field(default_factory=Row)

    @property
    def total(self) -> Row:
        out = Row()
        for row in self.calls.values():
            out.add(row)
        return out


def _normal(sql: str) -> str:
    return " ".join(sql.split()).rstrip(";").strip().lower()


def parse_call_stats(lines: list[str], sql: str) -> CallStats | None:
    """The statement's calls (dep=0 calls on a cursor whose text is `sql`) and the recursive
    SQL it caused; None when the statement is not in the lines.

    The lines may hold other statements too (autotrace's own readings). A cursor number is
    bound to a text by its latest `PARSING IN CURSOR` above, read in order (numbers are
    reused). Recursive calls are written before the call that caused them, so the dep>0
    calls since the last dep=0 call belong to the next dep=0 call."""
    want = _normal(sql)
    text_of: dict[str, str] = {}  # cursor -> the text it holds now
    reading: str | None = None
    parts: list[str] = []
    out, found = CallStats(), False
    pending = Row()  # dep>0 calls not yet claimed by a dep=0 call
    for raw in lines:
        line = raw.rstrip("\n")
        if reading is not None:
            if line.startswith("END OF STMT"):
                text_of[reading] = _normal("\n".join(parts))
                reading = None
            else:
                parts.append(line)
            continue
        m = CURSOR_RE.match(line)
        if m:
            reading, parts = m.group(1), []
            continue
        m = CALL_RE.match(line)
        if not m:
            continue
        c, e, p, cr, cu, r = (int(m.group(i)) for i in range(3, 9))
        row, dep = Row(1, c, e, p, cr, cu, r), int(m.group(9))
        if dep > 0:
            pending.add(row)
            continue
        if text_of.get(m.group(2)) == want:
            found = True
            out.calls[_KIND[m.group(1)]].add(row)
            out.recursive.add(pending)
        pending = Row()
    return out if found else None


HEADER = f"{'call':<9}{'count':>6}{'cpu':>10}{'elapsed':>11}{'disk':>11}{'query':>11}{'current':>11}{'rows':>11}"
DASHES = f"{'-' * 7}  {'-' * 6}{'  ' + '-' * 8}{' ' + '-' * 10}" + f"{' ' + '-' * 10}" * 4


def _line(label: str, row: Row) -> str:
    return (f"{label:<9}{row.count:>6}{row.cpu / 1e6:>10.2f}{row.elapsed / 1e6:>11.2f}"
            f"{row.disk:>11}{row.query:>11}{row.current:>11}{row.rows:>11}")


def format_call_stats(stats: CallStats) -> list[str]:
    """The table as TKPROF prints it, then the recursive SQL's totals on one line."""
    out = [HEADER, DASHES] + [_line(c, stats.calls[c]) for c in CALLS]
    out += [DASHES, _line("total", stats.total)]
    if stats.recursive.count:
        out += ["", _line("recursive", stats.recursive)]
    return out
