"""The UPDATE for a value edited in the result grid (no UI state, so it is testable).

The grid's query must read one plain table (`select … from emp where …`): no join, subquery,
union, grouping, DISTINCT or CTE. The row is found by its ROWID when the result has one,
else by the values of every column it shows that is a real column of the table (aliases and
expressions are left out, LOBs and bytes cannot be compared). On Oracle `and rownum = 1` keeps
a duplicate row from being changed twice.

    update emp set ename = 'KIM'
     where empno = 7369
       and ename = 'SMITH';
"""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation

from .grid import DATE_TEXT_RE, is_number
from .objects import SQL_WORDS, blank_noise, scan
from .statements import read_only

IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$#]*\Z")
# a query whose rows are not one table's rows, one for one
NOT_UPDATABLE_RE = re.compile(r"\b(join|union|intersect|minus|except|group\s+by|having|distinct|unique|"
                              r"connect\s+by|pivot|unpivot|model)\b|\(\s*select\b|^\s*with\b", re.I)
# Oracle types (grid.types labels) a WHERE cannot compare, or not reliably
NO_COMPARE = frozenset({"CL", "NCL", "BB", "BFL", "J", "X", "V", "OBJ", "CUR", "TZ", "TL", "IDS", "IYM"})
ORACLE_DATE_FMT = {10: "YYYY-MM-DD", 16: "YYYY-MM-DD HH24:MI", 19: "YYYY-MM-DD HH24:MI:SS"}


class EditError(Exception):
    """Why the edit cannot become an UPDATE; shown on the status line."""


def source_table(sql: str) -> str:
    """The one table the query reads, as written in it."""
    plain = blank_noise(sql).strip().rstrip(";")
    if ";" in plain:
        raise EditError("the result comes from several statements")
    if not read_only(sql):
        raise EditError("only a query's result can be edited")
    if NOT_UPDATABLE_RE.search(plain):
        raise EditError("only a query on one table can be edited (no join, subquery, union, group by or distinct)")
    found = scan(sql, plain)
    if found.opaque or len(found.tables) != 1:
        raise EditError("only a query on one plain table can be edited")
    return found.tables[0][1]


def quote_ident(name: str, kind: str) -> str:
    """A column name as the driver gave it: bare when that reads the same, else in quotes."""
    bare = IDENT_RE.match(name) and name.lower() not in SQL_WORDS and (
        kind == "sqlite" or (kind == "oracle" and name == name.upper())
        or (kind == "postgres" and name == name.lower()))
    return name if bare else '"' + name.replace('"', '""') + '"'


def quote_text(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _oracle_datetime(text: str, label: str) -> str:
    """TO_DATE / TO_TIMESTAMP for `2026-11-18`, `2026-11-18 05:00`, `… 05:00:00`, `….123`."""
    text = text.strip().replace("T", " ", 1)
    if not DATE_TEXT_RE.fullmatch(text) or len(text) < 10:
        raise EditError(f"not a date: {text!r} (write it as YYYY-MM-DD HH24:MI:SS)")
    if len(text) > 19 or label == "T":
        base = ORACLE_DATE_FMT[19] if len(text) >= 19 else ORACLE_DATE_FMT.get(len(text), ORACLE_DATE_FMT[10])
        fmt = base + (".FF" if len(text) > 19 else "")
        return f"TO_TIMESTAMP({quote_text(text)}, '{fmt}')"
    return f"TO_DATE({quote_text(text)}, '{ORACLE_DATE_FMT[len(text)]}')"


def literal(v, kind: str, label: str = "") -> str:
    """A fetched value as SQL, to compare a column against."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return ("TRUE" if v else "FALSE") if kind == "postgres" else ("1" if v else "0")
    if is_number(v):
        return repr(v) if isinstance(v, float) else str(v)
    if isinstance(v, (bytes, bytearray)):
        raise EditError("binary values cannot be compared")
    if isinstance(v, dt.datetime):
        text = v.isoformat(" ")
        return _oracle_datetime(text, label or "T") if kind == "oracle" else quote_text(text)
    if isinstance(v, dt.date):
        return _oracle_datetime(v.isoformat(), label or "D") if kind == "oracle" else quote_text(v.isoformat())
    if kind == "oracle" and label in ("D", "T") and isinstance(v, str) and DATE_TEXT_RE.fullmatch(v):
        return _oracle_datetime(v, label)  # a restored result keeps dates as text
    return quote_text(str(v))


def new_value(text: str, kind: str, col_kind: str | None, label: str, numeric: bool):
    """What was typed, as SQL and as the value the grid then shows. `NULL` sets NULL."""
    if text.strip().upper() == "NULL":
        return "NULL", None
    if col_kind == "bytes" or label in ("BB", "R", "BFL"):
        raise EditError("binary values cannot be edited here")
    if numeric or col_kind == "number":
        if not text.strip():
            return "NULL", None
        try:
            d = Decimal(text.strip())
        except InvalidOperation:
            raise EditError(f"not a number: {text.strip()!r}") from None
        if not d.is_finite():
            raise EditError(f"not a number: {text.strip()!r}")
        shown = int(d) if d == d.to_integral_value() and "." not in text and "e" not in text.lower() else d
        return str(d), shown
    if col_kind == "bool":
        word = text.strip().lower()
        if word in ("true", "t", "1", "yes", "y"):
            return ("TRUE" if kind == "postgres" else "1"), True
        if word in ("false", "f", "0", "no", "n"):
            return ("FALSE" if kind == "postgres" else "0"), False
        raise EditError(f"not a boolean: {text.strip()!r}")
    if col_kind == "date" and not text.strip():
        return "NULL", None
    if kind == "oracle" and label in ("D", "T"):
        return _oracle_datetime(text, label), text.strip()
    if kind == "oracle" and text == "":
        return "''", None  # Oracle keeps '' as NULL
    return quote_text(text), text


def _comparable(v, label: str, col_kind: str | None) -> bool:
    return not (isinstance(v, (bytes, bytearray, dt.timedelta)) or col_kind == "bytes" or label in NO_COMPARE)


def build_update(kind: str, table: str, columns: list[str], row: tuple, kinds: list, types: list,
                 numeric: list, col: int, text: str, table_columns: set[str] | None = None):
    """(the UPDATE, the edited column's new value as the grid shows it).

    `table_columns` (lower case) are the table's real columns when known: the edited column
    must be one, and only those find the row."""
    real = (lambda name: name.lower() in table_columns) if table_columns else (lambda name: IDENT_RE.match(name))
    name = columns[col]
    kinds = kinds or [None] * len(columns)
    types = types or [""] * len(columns)
    if name.lower() in ("rowid", "ctid", "oid"):
        raise EditError(f"{name} cannot be edited")
    if not real(name):
        raise EditError(f"{name} is not a column of {table} (an alias or an expression)")
    set_sql, shown = new_value(text, kind, kinds[col], types[col], numeric[col])

    rowid = next((i for i, c in enumerate(columns) if c.lower() == "rowid"), None)
    if rowid is not None and row[rowid] is not None:
        conds = [f"rowid = {literal(row[rowid], kind)}"]
    else:
        picked = [i for i, c in enumerate(columns)
                  if real(c) and _comparable(row[i], types[i], kinds[i])]
        exact = [i for i in picked if not isinstance(row[i], float)]
        picked = exact or picked  # floats compare only when nothing else is there
        seen, conds = set(), []
        for i in picked:
            if columns[i].lower() in seen:
                continue  # `select t.*, name` shows a column twice
            seen.add(columns[i].lower())
            q = quote_ident(columns[i], kind)
            conds.append(f"{q} IS NULL" if row[i] is None else f"{q} = {literal(row[i], kind, types[i])}")
        if not conds:
            raise EditError("no column to find the row by")
        if kind == "oracle":
            conds.append("rownum = 1")
    where = "\n   and ".join(conds)
    sql = f"update {table} set {quote_ident(name, kind)} = {set_sql}\n where {where};"
    return sql, shown
