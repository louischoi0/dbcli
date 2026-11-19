"""Database adapters: SQLite, PostgreSQL (psycopg) and Oracle (python-oracledb thin)."""
from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass

from .config import Profile
from .trace import CURSOR_GRANT, STATS_GRANT, STATS_SQL, explainable, format_trace, sql_id, stats_delta

# Every statement dbcli sends on its own (catalog reads for completion and underlines, the
# schema browser, the monitor, autotrace's helpers) starts with this comment, so the monitor
# (and a DBA) can tell them from what the user ran. V$SQL / pg_stat_activity keep it.
BG_MARK = "/* dbcli:bg */ "

MAX_ROWS = 1000  # a traced run fetches this many at most
PAGE_ROWS = 500  # a query's first fetch; scrolling down fetches the next pages from its open cursor
# PL/SQL needs its trailing ';', plain SQL sent to Oracle must not have one.
PLSQL_RE = re.compile(
    r"\s*(begin|declare|create\s+(or\s+replace\s+)?(procedure|function|package|trigger|type))\b", re.I)


# Errors that mean the statement itself is wrong (syntax, or a name the parser cannot resolve):
# such a statement is not kept in the history.
ORACLE_PARSE_RE = re.compile(r"\b(ORA-009\d\d|ORA-01747|ORA-01756|ORA-06550|PLS-\d{5})\b")
SQLITE_PARSE_RE = re.compile(r"syntax error|no such (table|column|function)|incomplete input|unrecognized token",
                             re.I)


HELP_LINE_RE = re.compile(r"^\s*Help:\s*https?://\S+\s*$", re.I)


CONNECTION_LOST_RE = re.compile(r"\b(DPY-1001|DPY-4011|DPY-6005|DPI-1080|ORA-03113|ORA-03114|ORA-03135|"
                                r"ORA-12170|ORA-12537|ORA-12541|ORA-12514)\b")


def is_connection_error(exc: BaseException) -> bool:
    """The connection itself is gone (not the statement's fault): the next run reconnects."""
    if type(exc).__name__ in ("OperationalError", "InterfaceError") and "psycopg" in type(exc).__module__:
        return True
    return bool(CONNECTION_LOST_RE.search(str(exc)))


def error_text(exc: BaseException) -> str:
    """A driver error as shown: its code and message, without the help link Oracle adds
    (`Help: https://docs.oracle.com/error-help/db/ora-00900/`)."""
    lines = [l for l in str(exc).splitlines() if not HELP_LINE_RE.match(l)]
    return "\n".join(lines).strip()


def is_parse_error(exc: BaseException) -> bool:
    """True for syntax and parse errors: ORA-009xx (ORA-00900 invalid SQL, 00904 invalid
    identifier, 00933 not properly ended, 00942 no such table …), PL/SQL compile errors,
    PostgreSQL SQLSTATE class 42, SQLite's syntax and "no such" errors."""
    sqlstate = getattr(exc, "sqlstate", None)
    if isinstance(sqlstate, str):
        return sqlstate.startswith("42")
    text = str(exc)
    return bool(ORACLE_PARSE_RE.search(text) or (isinstance(exc, sqlite3.Error) and SQLITE_PARSE_RE.search(text)))


def prepare_statement(text: str, kind: str) -> str | None:
    """SQL to send for the whole input when the user runs it (Ctrl+Enter), or None if empty.

    A SQL*Plus style lone '/' on the last line is dropped for Oracle, so PL/SQL
    copied from scripts still works.
    """
    body = text.strip()
    if kind == "oracle":
        lines = body.splitlines()
        if lines and lines[-1].strip() == "/":
            body = "\n".join(lines[:-1]).rstrip()
    return body or None


DDL_RE = re.compile(r"\s*(create|drop|alter|rename)\b", re.I)


def changes_schema(sql: str) -> bool:
    """True when the table list may have changed and should be reloaded."""
    return bool(DDL_RE.match(sql))


@dataclass
class Result:
    columns: list[str]
    rows: list[tuple]
    rowcount: int
    truncated: bool = False
    kinds: list[str | None] | None = None  # each column's type, as column_kind names it
    types: list[str] | None = None  # each column's type, short (N, VCH, T …), as type_label gives it
    more: "Pager | None" = None  # the rest of the rows, when there are more than the first page


class Pager:
    """The rest of a query's rows, fetched on demand from its still open cursor."""

    def __init__(self, cur, peeked: list[tuple]):
        self._cur, self._peeked, self.done = cur, list(peeked), False

    def fetch(self, n: int = PAGE_ROWS) -> list[tuple]:
        """The next n rows (fewer at the end); `done` once nothing is left, the cursor closed."""
        if self.done:
            return []
        rows = self._peeked + self._cur.fetchmany(n + 1 - len(self._peeked))
        self._peeked = rows[n:]
        if len(rows) <= n:
            self.close()
        return rows[:n]

    def close(self) -> None:
        if not self.done:
            self.done = True
            try:
                self._cur.close()
            except Exception:
                pass


# Oracle's driver types, short, for the result header: DB_TYPE_<key> -> label
TYPE_LABELS = {
    "NUMBER": "N", "BINARY_INTEGER": "N", "BINARY_FLOAT": "BF", "BINARY_DOUBLE": "BD",
    # CLOB / BLOB arrive as LONG / LONG_RAW (LOBs are fetched as text and bytes), so those
    # read as CL / BB: a real LONG column (rare, old) shows as CL too
    "VARCHAR": "VCH", "NVARCHAR": "NVCH", "CHAR": "CH", "NCHAR": "NCH", "LONG": "CL",
    "CLOB": "CL", "NCLOB": "NCL", "BLOB": "BB", "BFILE": "BFL", "RAW": "R", "LONG_RAW": "BB",
    "DATE": "D", "TIMESTAMP": "T", "TIMESTAMP_TZ": "TZ", "TIMESTAMP_LTZ": "TL",
    "INTERVAL_DS": "IDS", "INTERVAL_YM": "IYM", "ROWID": "RID", "UROWID": "URID",
    "BOOLEAN": "B", "JSON": "J", "XMLTYPE": "X", "VECTOR": "V", "OBJECT": "OBJ", "CURSOR": "CUR",
}


def type_label(type_code) -> str:
    """N, VCH, CH, BB, R, T … for a column's driver type; "" when the driver names none."""
    name = getattr(type_code, "name", None)
    if not isinstance(name, str):
        return ""
    key = name.upper().removeprefix("DB_TYPE_")
    return TYPE_LABELS.get(key, key[:4])


def column_kind(type_code) -> str | None:
    """number | text | date | bytes | bool from a driver's column type, None when unknown
    (then each value's own type decides). Oracle's types are named (DB_TYPE_NUMBER …);
    PostgreSQL's are OIDs, SQLite has none."""
    name = getattr(type_code, "name", None)
    if not isinstance(name, str):
        return None
    name = name.upper()
    for words, kind in ((("BOOL",), "bool"),
                        (("NUMBER", "INT", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC", "REAL"), "number"),
                        (("DATE", "TIME", "INTERVAL"), "date"),
                        (("RAW", "BLOB", "BFILE", "BINARY", "BYTEA"), "bytes"),
                        (("CHAR", "CLOB", "LONG", "TEXT", "JSON", "XML", "ROWID", "VECTOR"), "text")):
        if any(w in name for w in words):
            return kind
    return None


def _fetch(cur, paged: bool = False) -> Result:
    """The statement's outcome. Paged: the first PAGE_ROWS rows, and a Pager holding the open
    cursor when there are more (the caller must not close it then); else up to MAX_ROWS."""
    if cur.description is None:
        return Result([], [], cur.rowcount)
    first = PAGE_ROWS if paged else MAX_ROWS
    rows = cur.fetchmany(first + 1)
    more = Pager(cur, rows[first:]) if paged and len(rows) > first else None
    return Result([d[0] for d in cur.description], rows[:first], min(len(rows), first),
                  truncated=len(rows) > first and not paged, kinds=[column_kind(d[1]) for d in cur.description],
                  types=[type_label(d[1]) for d in cur.description], more=more)


def _run_paged(cur, sql: str, *args) -> Result:
    """Execute on a cursor of its own and fetch the first page; the cursor stays open only
    while a Pager has more to give."""
    try:
        cur.execute(sql, *args)
        res = _fetch(cur, paged=True)
    except BaseException:
        cur.close()
        raise
    if res.more is None:
        cur.close()
    return res


class SqliteDB:
    kind = "sqlite"

    def __init__(self, path: str):
        self.name = path
        # queries run in a worker thread so the UI stays responsive
        self._con = sqlite3.connect(os.path.expanduser(path), check_same_thread=False)
        self.autocommit = True

    def set_autocommit(self, on: bool) -> None:
        raise ValueError("SQLite here commits every statement; autocommit cannot be turned off")

    def execute(self, sql: str) -> Result:
        res = _run_paged(self._con.cursor(), sql.strip().rstrip(";"))
        if not res.columns:
            self._con.commit()
        return res

    def object_names(self) -> set[str]:
        """Every table and view name a statement may use, lower case."""
        names = {r[0].lower() for r in self._con.execute(BG_MARK + "select name from sqlite_master "
                                                          "where type in ('table', 'view')")}
        return names | {"sqlite_master", "sqlite_schema", "sqlite_temp_master", "sqlite_sequence"}

    def columns_of(self, table: str) -> set[str] | None:
        """The table's column names, lower case; None when it cannot be told."""
        cols = {r[1].lower() for r in self._con.execute(BG_MARK + f'pragma table_info("{table}")')}
        return cols or None

    def relations(self) -> list[tuple[str, str]]:
        return list(self._con.execute(
            BG_MARK + "select name, type from sqlite_master where type in ('table','view') "
            "and name not like 'sqlite_%' order by name"))

    def table_columns(self) -> list[tuple[str, str]]:
        """(table, column) of every table and view: the catalog for completion and underlines."""
        out = []
        for name, _ in self.relations():
            out += [(name, r[1]) for r in self._con.execute(BG_MARK + f'pragma table_info("{name}")')]
        return out

    def ping(self) -> None:
        self._con.execute(BG_MARK + "select 1")

    def cancel(self) -> None:
        """Stop the running statement (from another thread): it fails with "interrupted"."""
        self._con.interrupt()

    def close(self) -> None:
        self._con.close()


class PostgresDB:
    kind = "postgres"

    def __init__(self, p: Profile):
        import psycopg

        o = p.options
        kw = {k: o[k] for k in ("host", "port", "dbname", "user") if k in o}
        if (pw := p.password()) is not None:
            kw["password"] = pw
        self.name = p.name
        self.autocommit = bool(o.get("autocommit", True))
        self._con = psycopg.connect(o.get("dsn", ""), autocommit=self.autocommit,
                                    connect_timeout=10, **kw)

    def set_autocommit(self, on: bool) -> None:
        self._con.autocommit = on  # psycopg refuses while a transaction is open
        self.autocommit = on

    def execute(self, sql: str) -> Result:
        return _run_paged(self._con.cursor(), sql)

    def object_names(self) -> set[str]:
        """Every table and view name visible to the user, lower case (any schema)."""
        with self._con.cursor() as cur:
            cur.execute(BG_MARK + "select relname from pg_class where relkind in ('r', 'v', 'm', 'p', 'f')")
            return {r[0].lower() for r in cur.fetchall()}

    def columns_of(self, table: str) -> set[str] | None:
        with self._con.cursor() as cur:
            cur.execute(BG_MARK + "select column_name from information_schema.columns where lower(table_name) = lower(%s)",
                        (table,))
            return {r[0].lower() for r in cur.fetchall()} or None

    def relations(self) -> list[tuple[str, str]]:
        with self._con.cursor() as cur:  # tables and views of the search_path schema
            cur.execute(BG_MARK + "select table_name, case when table_type = 'VIEW' then 'view' else 'table' end "
                        "from information_schema.tables where table_schema = current_schema() order by 1")
            return [tuple(r) for r in cur.fetchall()]

    def table_columns(self) -> list[tuple[str, str]]:
        with self._con.cursor() as cur:
            cur.execute(BG_MARK + "select table_name, column_name from information_schema.columns "
                        "where table_schema not in ('pg_catalog', 'information_schema') limit 50000")
            return [tuple(r) for r in cur.fetchall()]

    def ping(self) -> None:
        self._con.execute(BG_MARK + "select 1")

    def cancel(self) -> None:
        """Stop the running statement (from another thread): it fails with QueryCanceled."""
        self._con.cancel_safe()

    def close(self) -> None:
        self._con.close()


class OracleDB:
    kind = "oracle"

    def __init__(self, p: Profile):
        import oracledb

        oracledb.defaults.fetch_lobs = False  # CLOB/BLOB come back as str/bytes
        o = p.options
        self.name = p.name
        self._con = oracledb.connect(user=o.get("user"), password=p.password(), dsn=o.get("dsn"),
                                     tcp_connect_timeout=10)
        # Oracle convention: nothing is committed until you run `commit;`
        self.autocommit = bool(o.get("autocommit", False))
        self._con.autocommit = self.autocommit

    def set_autocommit(self, on: bool) -> None:
        # changes made before turning it on stay pending until the next statement commits
        self._con.autocommit = on
        self.autocommit = on

    def execute(self, sql: str) -> Result:
        sql = sql.strip()
        if not PLSQL_RE.match(sql):
            sql = sql.rstrip(";")
        return _run_paged(self._con.cursor(), sql)

    def execute_traced(self, sql: str) -> tuple[Result, list[str]]:
        """Run like `execute` and also return autotrace's plan and statistics lines."""
        sql = sql.strip()
        if not PLSQL_RE.match(sql):
            sql = sql.rstrip(";")
        with self._con.cursor() as cur:
            # row-source statistics for DISPLAY_CURSOR's A-Rows, for this statement only
            cur.execute(BG_MARK + "alter session set statistics_level = all")
            try:
                before = self._stats(cur)
                warm = self._stats(cur) if before is not None else None
                cur.execute(sql)
                res = _fetch(cur)
                after = self._stats(cur) if warm is not None else None
            finally:
                cur.execute(BG_MARK + "alter session set statistics_level = typical")
            stats = stats_delta(before, warm, after) if after is not None else None
            plan, plan_note, actual, actual_note = None, "", None, ""
            if explainable(sql):
                actual, actual_note = self._actual_plan(cur, sql)
                # EXPLAIN PLAN writes PLAN_TABLE: inside a savepoint it rolls back, so it neither
                # leaves a transaction (and its lock) open nor touches the user's own changes
                try:
                    cur.execute(BG_MARK + "savepoint dbcli_explain")
                    cur.execute(BG_MARK + "explain plan for " + sql)
                    cur.execute(BG_MARK + "select plan_table_output from table(dbms_xplan.display())")
                    plan = [r[0] or "" for r in cur.fetchall()]
                except Exception as e:
                    plan_note = f"(execution plan error: {str(e).splitlines()[0]})"
                finally:
                    try:
                        cur.execute(BG_MARK + "rollback to savepoint dbcli_explain")
                    except Exception:
                        pass
        stats_note = "" if stats is not None else f"(no statistics: v$mystat cannot be read)\n  {STATS_GRANT}"
        return res, format_trace(plan, stats, res.rowcount, plan_note, stats_note, actual, actual_note)

    @staticmethod
    def _actual_plan(cur, sql: str) -> tuple[list[str] | None, str]:
        """The plan the statement really ran with, from the cursor cache (its latest child)."""
        sid = sql_id(sql)
        try:
            cur.execute(BG_MARK + "select child_number from v$sql where sql_id = :i "
                        "order by last_active_time desc, child_number desc fetch first 1 row only", i=sid)
            row = cur.fetchone()
            if row is None:
                return None, f"(SQL_ID {sid} is not in the cursor cache)"
            cur.execute(BG_MARK + "select plan_table_output from table(dbms_xplan.display_cursor(:i, :c, 'ALLSTATS LAST'))",
                        i=sid, c=row[0])
            return [r[0] or "" for r in cur.fetchall()], ""
        except Exception as e:
            return None, f"(no actual plan: {str(e).splitlines()[0]})\n  {CURSOR_GRANT} to <user>;"

    @staticmethod
    def _stats(cur) -> dict[str, int] | None:
        """The session's autotrace counters, or None without the privilege to read them."""
        try:
            cur.execute(BG_MARK + STATS_SQL)
            return {name: int(value) for name, value in cur.fetchall()}
        except Exception:
            return None

    def object_names(self) -> set[str]:
        """Every table, view and synonym the user can see (DUAL, V$ views included), lower case."""
        with self._con.cursor() as cur:
            cur.execute(BG_MARK + "select distinct object_name from all_objects "
                        "where object_type in ('TABLE', 'VIEW', 'SYNONYM', 'MATERIALIZED VIEW')")
            return {r[0].lower() for r in cur.fetchall()}

    def columns_of(self, table: str) -> set[str] | None:
        """The columns of the user's own table or view of that name, or of what a private or
        public synonym of that name points to (DUAL, V$ views), lower case."""
        with self._con.cursor() as cur:
            cur.execute(BG_MARK + "select column_name from all_tab_columns where (owner, table_name) in ("
                        " select user, :t from dual union all"
                        " select table_owner, table_name from all_synonyms"
                        " where synonym_name = :t and owner in (user, 'PUBLIC'))", t=table.upper())
            return {r[0].lower() for r in cur.fetchall()} or None

    def relations(self) -> list[tuple[str, str]]:
        with self._con.cursor() as cur:
            cur.execute(BG_MARK + "select table_name, 'table' from user_tables "
                        "union all select view_name, 'view' from user_views order by 1")
            return [tuple(r) for r in cur.fetchall()]

    def table_columns(self) -> list[tuple[str, str]]:
        with self._con.cursor() as cur:
            cur.execute(BG_MARK + "select table_name, column_name from user_tab_columns "
                        "fetch first 50000 rows only")
            return [tuple(r) for r in cur.fetchall()]

    def ping(self) -> None:
        self._con.ping()

    def cancel(self) -> None:
        """Stop the running statement (from another thread): it fails with ORA-01013."""
        self._con.cancel()

    def close(self) -> None:
        self._con.close()


def open_db(p: Profile):
    if p.type == "sqlite":
        db = SqliteDB(p.options.get("path", ":memory:"))
        db.name = p.name
        return db
    return {"postgres": PostgresDB, "oracle": OracleDB}[p.type](p)


def format_table(res: Result) -> str:
    if not res.columns:
        return f"OK, {res.rowcount} row(s) affected"
    cells = [["NULL" if v is None else str(v).replace("\n", "\\n") for v in row] for row in res.rows]
    widths = [max([len(c)] + [len(r[i]) for r in cells]) for i, c in enumerate(res.columns)]
    line = lambda vals: " | ".join(v.ljust(w) for v, w in zip(vals, widths)).rstrip()
    out = [line(res.columns), "-+-".join("-" * w for w in widths)]
    out += [line(r) for r in cells]
    more = f", first {MAX_ROWS} shown" if res.truncated else ""
    out.append(f"({res.rowcount} row{'s' if res.rowcount != 1 else ''}{more})")
    return "\n".join(out)
