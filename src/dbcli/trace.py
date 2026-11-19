"""autotrace for Oracle (the F8 switch): the execution plan and session statistics of a statement.

Like SQL*Plus, the plan comes from EXPLAIN PLAN + DBMS_XPLAN.DISPLAY and the statistics
are the change in the session's V$MYSTAT counters while the statement ran. On top of that
the statement runs with STATISTICS_LEVEL = ALL, so DBMS_XPLAN.DISPLAY_CURSOR shows the plan
it really took with actual rows (A-Rows), times and buffers.
"""
from __future__ import annotations

import hashlib
import re

RULE = "-" * 58
# the statistics SQL*Plus prints, in its order
STAT_NAMES = (
    "recursive calls",
    "db block gets",
    "consistent gets",
    "physical reads",
    "redo size",
    "bytes sent via SQL*Net to client",
    "bytes received via SQL*Net from client",
    "SQL*Net roundtrips to/from client",
    "sorts (memory)",
    "sorts (disk)",
)
STATS_SQL = ("select n.name, s.value from v$mystat s join v$statname n on n.statistic# = s.statistic# "
             "where n.name in (" + ", ".join(f"'{n}'" for n in STAT_NAMES) + ")")
STATS_GRANT = "grant select on v_$mystat to <user>;  -- as SYS (v_$statname too)"
CURSOR_GRANT = "grant select on v_$sql, v_$sql_plan, v_$sql_plan_statistics_all, v_$session"
SQL_ID_ALPHABET = "0123456789abcdfghjkmnpqrstuvwxyz"

EXPLAINABLE_RE = re.compile(r"\s*(select|with|insert|update|delete|merge)\b", re.I)


def explainable(sql: str) -> bool:
    """EXPLAIN PLAN takes queries and DML, not DDL or PL/SQL."""
    return bool(EXPLAINABLE_RE.match(sql))


def sql_id(text: str) -> str:
    """Oracle's SQL_ID of a statement text, as V$SQL has it (the low 64 bits of its MD5, base 32)."""
    h = hashlib.md5(text.encode() + b"\x00").digest()
    value = (int.from_bytes(h[8:12], "little") << 32) | int.from_bytes(h[12:16], "little")
    return "".join(SQL_ID_ALPHABET[(value >> (5 * i)) & 31] for i in range(12, -1, -1))


def stats_delta(before: dict[str, int], warm: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    """What the statement itself added to the counters.

    Reading the counters is itself a round trip that moves them. `before` and `warm`
    are two readings taken back to back, so their difference is the cost of one
    reading; it is taken off the change between `warm` and `after`.
    """
    out = {}
    for name in STAT_NAMES:
        reading = warm.get(name, 0) - before.get(name, 0)
        out[name] = max(0, after.get(name, 0) - warm.get(name, 0) - reading)
    return out


def format_trace(plan: list[str] | None, stats: dict[str, int] | None, rows: int,
                 plan_note: str = "", stats_note: str = "", actual: list[str] | None = None,
                 actual_note: str = "") -> list[str]:
    """The panel's lines, laid out like SQL*Plus autotrace output, the actual plan after the estimate."""
    out = ["Execution Plan", RULE]
    out += plan if plan else (plan_note or "(no execution plan for this statement)").splitlines()
    if actual or actual_note:
        out += ["", "Actual Plan (DISPLAY_CURSOR · ALLSTATS LAST)", RULE]
        out += actual if actual else actual_note.splitlines()
    out += ["", "Statistics", RULE]
    if stats is None:
        out += (stats_note or "(statistics could not be read)").splitlines()
    else:
        out += [f"{stats[name]:>11}  {name}" for name in STAT_NAMES]
    out.append(f"{rows:>11}  rows processed")
    return out
