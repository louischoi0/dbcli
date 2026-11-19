"""The schema browser's queries: object kinds, objects of a kind, an object's details, its DDL.

Each database answers the same four questions:

    kinds(db)                -> [(kind, count)]            TABLE 12, VIEW 3, INDEX 20, …
    objects(db, kind)        -> [(name, note)]             note: INVALID, a row count …
    detail(db, kind, name)   -> [Section]                  Columns, Indexes, Constraints, Triggers …
    ddl(db, kind, name)      -> str                        the CREATE statement

Oracle reads the user's own objects (USER_* views, DBMS_METADATA for the DDL); PostgreSQL the
current schema; SQLite its sqlite_master. A section whose query fails shows the error
instead, so one missing privilege never hides the rest.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .db import BG_MARK

ORACLE_KINDS = ("TABLE", "VIEW", "MATERIALIZED VIEW", "INDEX", "SEQUENCE", "SYNONYM", "TRIGGER",
                "PROCEDURE", "FUNCTION", "PACKAGE", "TYPE")
# DBMS_METADATA's names for them
ORACLE_DDL_TYPES = {"MATERIALIZED VIEW": "MATERIALIZED_VIEW", "PACKAGE": "PACKAGE", "TYPE": "TYPE"}


@dataclass
class Section:
    title: str
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    error: str = ""


def _query(con, sql: str, **binds) -> tuple[list[str], list[tuple]]:
    cur = con.cursor()
    try:
        sql = BG_MARK + sql
        cur.execute(sql, binds) if binds else cur.execute(sql)
        cols = [d[0].lower() for d in cur.description] if cur.description else []
        return cols, [tuple(r) for r in cur.fetchall()]
    finally:
        cur.close()


def _section(con, title: str, sql: str, **binds) -> Section:
    try:
        cols, rows = _query(con, sql, **binds)
        return Section(title, cols, rows)
    except Exception as e:
        return Section(title, error=str(e).splitlines()[0])


def _pairs(title: str, values: dict) -> Section:
    """A key/value section, the keys with a value only."""
    rows = [(k, v) for k, v in values.items() if v not in (None, "")]
    return Section(title, ["property", "value"], rows)


# --- Oracle -----------------------------------------------------------------

ORACLE_TYPE_SQL = """case
  when c.data_type in ('VARCHAR2', 'NVARCHAR2', 'CHAR', 'NCHAR') then c.data_type || '(' || c.char_length || ')'
  when c.data_type = 'RAW' then 'RAW(' || c.data_length || ')'
  when c.data_type = 'NUMBER' and c.data_precision is not null
    then 'NUMBER(' || c.data_precision || case when c.data_scale > 0 then ',' || c.data_scale end || ')'
  else c.data_type end"""


def _oracle_kinds(con) -> list[tuple[str, int]]:
    _, rows = _query(con, "select object_type, count(*) from user_objects group by object_type")
    counts = dict(rows)
    return [(k, int(counts.get(k, 0))) for k in ORACLE_KINDS]


def _oracle_objects(con, kind: str) -> list[tuple[str, str]]:
    _, rows = _query(con, "select object_name, status, to_char(last_ddl_time, 'YYYY-MM-DD HH24:MI') "
                          "from user_objects where object_type = :k order by object_name", k=kind)
    return [(name, "INVALID" if status != "VALID" else when or "") for name, status, when in rows]


def _oracle_columns(con, name: str) -> Section:
    return _section(con, "Columns", f"""
        select c.column_id "#", c.column_name name, {ORACLE_TYPE_SQL} type,
               decode(c.nullable, 'N', 'not null') nulls, cc.comments
        from user_tab_columns c
        left join user_col_comments cc on cc.table_name = c.table_name and cc.column_name = c.column_name
        where c.table_name = :n order by c.column_id""", n=name)


def _oracle_detail(con, kind: str, name: str) -> list[Section]:
    out: list[Section] = []
    if kind in ("TABLE", "VIEW", "MATERIALIZED VIEW"):
        if kind == "TABLE":
            try:
                _, rows = _query(con, "select t.num_rows, to_char(t.last_analyzed, 'YYYY-MM-DD HH24:MI'), "
                                      "t.tablespace_name, t.partitioned, c.comments from user_tables t "
                                      "left join user_tab_comments c on c.table_name = t.table_name "
                                      "where t.table_name = :n", n=name)
                if rows:
                    r = rows[0]
                    out.append(_pairs("Table", {"rows (statistics)": r[0], "analyzed": r[1],
                                                "tablespace": r[2], "partitioned": r[3], "comment": r[4]}))
            except Exception as e:
                out.append(Section("Table", error=str(e).splitlines()[0]))
        out.append(_oracle_columns(con, name))
        if kind != "VIEW":
            out.append(_section(con, "Indexes", """
                select i.index_name name, lower(i.uniqueness) uniqueness,
                       listagg(ic.column_name, ', ') within group (order by ic.column_position) columns,
                       lower(i.index_type) type, i.status
                from user_indexes i join user_ind_columns ic on ic.index_name = i.index_name
                where i.table_name = :n
                group by i.index_name, i.uniqueness, i.index_type, i.status order by 1""", n=name))
        out.append(_section(con, "Constraints", """
            select c.constraint_name name,
                   decode(c.constraint_type, 'P', 'primary key', 'U', 'unique', 'R', 'foreign key',
                          'C', 'check', 'V', 'view check', 'O', 'read only', c.constraint_type) type,
                   listagg(cc.column_name, ', ') within group (order by cc.position) columns,
                   max(r.table_name) references, max(c.search_condition_vc) condition, c.status
            from user_constraints c
            left join user_cons_columns cc on cc.constraint_name = c.constraint_name
            left join user_constraints r on r.constraint_name = c.r_constraint_name
            where c.table_name = :n
              and not (c.constraint_type = 'C' and c.generated = 'GENERATED NAME'
                       and c.search_condition_vc like '%IS NOT NULL')
            group by c.constraint_name, c.constraint_type, c.status order by 2, 1""", n=name))
        out.append(_section(con, "Triggers", """
            select trigger_name name, lower(trigger_type) timing, lower(triggering_event) event, status
            from user_triggers where table_name = :n order by 1""", n=name))
    elif kind == "INDEX":
        out.append(_section(con, "Index", """
            select i.table_name "table", lower(i.uniqueness) uniqueness, lower(i.index_type) type,
                   i.status, i.num_rows, to_char(i.last_analyzed, 'YYYY-MM-DD HH24:MI') analyzed
            from user_indexes i where i.index_name = :n""", n=name))
        out.append(_section(con, "Columns", """
            select column_position "#", column_name name, lower(descend) "order"
            from user_ind_columns where index_name = :n order by column_position""", n=name))
    elif kind == "SEQUENCE":
        out.append(_section(con, "Sequence", """
            select min_value, max_value, increment_by, last_number, cache_size, cycle_flag cycle, order_flag "order"
            from user_sequences where sequence_name = :n""", n=name))
    elif kind == "SYNONYM":
        out.append(_section(con, "Synonym", """
            select table_owner owner, table_name object, db_link from user_synonyms where synonym_name = :n""",
                            n=name))
    elif kind == "TRIGGER":
        out.append(_section(con, "Trigger", """
            select table_name "table", lower(trigger_type) timing, lower(triggering_event) event, status,
                   when_clause "when" from user_triggers where trigger_name = :n""", n=name))
    elif kind in ("PROCEDURE", "FUNCTION", "PACKAGE", "TYPE"):
        if kind in ("PROCEDURE", "FUNCTION"):
            out.append(_section(con, "Arguments", """
                select position "#", nvl(argument_name, '(return)') name, data_type type,
                       lower(in_out) "in/out", defaulted
                from user_arguments where object_name = :n and package_name is null
                  and data_level = 0 order by position""", n=name))
        if kind == "PACKAGE":
            out.append(_section(con, "Subprograms", """
                select procedure_name name, subprogram_id "#" from user_procedures
                where object_name = :n and procedure_name is not null order by subprogram_id""", n=name))
    # compile errors, for anything that has them
    if kind in ("VIEW", "TRIGGER", "PROCEDURE", "FUNCTION", "PACKAGE", "TYPE"):
        errors = _section(con, "Errors", """
            select type, line, position col, text from user_errors where name = :n
            order by sequence""", n=name)
        if errors.rows or errors.error:
            out.append(errors)
    return out


def _oracle_ddl(con, kind: str, name: str) -> str:
    cur = con.cursor()
    try:
        cur.execute(BG_MARK + """begin
            dbms_metadata.set_transform_param(dbms_metadata.session_transform, 'SEGMENT_ATTRIBUTES', false);
            dbms_metadata.set_transform_param(dbms_metadata.session_transform, 'STORAGE', false);
            dbms_metadata.set_transform_param(dbms_metadata.session_transform, 'SQLTERMINATOR', true);
            dbms_metadata.set_transform_param(dbms_metadata.session_transform, 'PRETTY', true);
        end;""")
        cur.execute(BG_MARK + "select dbms_metadata.get_ddl(:t, :n) from dual",
                    t=ORACLE_DDL_TYPES.get(kind, kind), n=name)
        text = cur.fetchone()[0]
        return (text.read() if hasattr(text, "read") else text or "").strip()
    finally:
        cur.close()


# --- SQLite -----------------------------------------------------------------

SQLITE_KINDS = ("TABLE", "VIEW", "INDEX", "TRIGGER")


def _sqlite_kinds(con):
    _, rows = _query(con, "select upper(type), count(*) from sqlite_master where name not like 'sqlite_%' group by type")
    counts = dict(rows)
    return [(k, int(counts.get(k, 0))) for k in SQLITE_KINDS]


def _sqlite_objects(con, kind):
    _, rows = _query(con, "select name, tbl_name from sqlite_master where upper(type) = ? and name not like 'sqlite_%' "
                          "order by name".replace("?", f"'{kind}'"))
    return [(n, t if kind in ("INDEX", "TRIGGER") else "") for n, t in rows]


def _sqlite_detail(con, kind, name):
    quoted = '"' + name.replace('"', '""') + '"'
    if kind in ("TABLE", "VIEW"):
        out = [_section(con, "Columns", f"select cid + 1 as \"#\", name, type, case \"notnull\" when 1 then 'not null' end as nulls, "
                                        f"dflt_value as \"default\", case when pk > 0 then 'pk' end as key "
                                        f"from pragma_table_info('{name}')")]
        if kind == "TABLE":
            out.append(_section(con, "Indexes", f"select name, case \"unique\" when 1 then 'unique' end as uniqueness, origin "
                                                f"from pragma_index_list('{name}')"))
            out.append(_section(con, "Triggers", "select name from sqlite_master where type = 'trigger' "
                                                 f"and tbl_name = '{name}'"))
        return out
    if kind == "INDEX":
        return [_section(con, "Columns", f"select seqno + 1 as \"#\", name from pragma_index_info('{name}')")]
    return [_section(con, "Trigger", f"select tbl_name as \"table\" from sqlite_master where name = '{name}'")]


def _sqlite_ddl(con, kind, name):
    _, rows = _query(con, f"select sql from sqlite_master where name = '{name}'")
    return (rows[0][0] or "").strip() + ";" if rows else ""


# --- PostgreSQL --------------------------------------------------------------

PG_KINDS = ("TABLE", "VIEW", "INDEX", "SEQUENCE", "FUNCTION")
PG_LISTS = {
    "TABLE": "select table_name from information_schema.tables where table_schema = current_schema() "
             "and table_type = 'BASE TABLE' order by 1",
    "VIEW": "select table_name from information_schema.views where table_schema = current_schema() order by 1",
    "INDEX": "select indexname from pg_indexes where schemaname = current_schema() order by 1",
    "SEQUENCE": "select sequence_name from information_schema.sequences where sequence_schema = current_schema() order by 1",
    "FUNCTION": "select distinct routine_name from information_schema.routines where routine_schema = current_schema() order by 1",
}


def _pg_kinds(con):
    return [(k, len(_query(con, PG_LISTS[k])[1])) for k in PG_KINDS]


def _pg_objects(con, kind):
    return [(r[0], "") for r in _query(con, PG_LISTS[kind])[1]]


def _pg_detail(con, kind, name):
    if kind in ("TABLE", "VIEW"):
        out = [_section(con, "Columns", "select ordinal_position as \"#\", column_name as name, data_type as type, "
                                        "case is_nullable when 'NO' then 'not null' end as nulls, column_default as \"default\" "
                                        "from information_schema.columns where table_schema = current_schema() "
                                        "and table_name = %(n)s order by 1", n=name)]
        if kind == "TABLE":
            out.append(_section(con, "Indexes", "select indexname as name, indexdef as definition from pg_indexes "
                                                "where schemaname = current_schema() and tablename = %(n)s order by 1", n=name))
            out.append(_section(con, "Constraints", "select conname as name, pg_get_constraintdef(c.oid) as definition "
                                                    "from pg_constraint c join pg_class t on t.oid = c.conrelid "
                                                    "where t.relname = %(n)s order by 1", n=name))
            out.append(_section(con, "Triggers", "select trigger_name as name, action_timing as timing, "
                                                 "event_manipulation as event from information_schema.triggers "
                                                 "where event_object_table = %(n)s order by 1", n=name))
        return out
    if kind == "INDEX":
        return [_section(con, "Index", "select tablename as \"table\", indexdef as definition from pg_indexes "
                                       "where indexname = %(n)s", n=name)]
    if kind == "SEQUENCE":
        return [_section(con, "Sequence", "select data_type, start_value, increment, maximum_value from "
                                          "information_schema.sequences where sequence_name = %(n)s", n=name)]
    return [_section(con, "Function", "select routine_type as type, data_type as returns from information_schema.routines "
                                      "where routine_name = %(n)s", n=name)]


def _pg_ddl(con, kind, name):
    if kind == "VIEW":
        return f"create or replace view {name} as\n" + _query(con, "select pg_get_viewdef(%(n)s::regclass, true)", n=name)[1][0][0]
    if kind == "INDEX":
        return _query(con, "select indexdef from pg_indexes where indexname = %(n)s", n=name)[1][0][0] + ";"
    if kind == "FUNCTION":
        rows = _query(con, "select pg_get_functiondef(p.oid) from pg_proc p where p.proname = %(n)s", n=name)[1]
        return "\n\n".join(r[0] for r in rows)
    if kind == "TABLE":  # PostgreSQL has no CREATE TABLE generator: built from the columns and constraints
        cols = _query(con, "select column_name, data_type, is_nullable, column_default from information_schema.columns "
                           "where table_schema = current_schema() and table_name = %(n)s order by ordinal_position", n=name)[1]
        cons = _query(con, "select conname, pg_get_constraintdef(c.oid) from pg_constraint c join pg_class t "
                           "on t.oid = c.conrelid where t.relname = %(n)s", n=name)[1]
        lines = [f"  {c} {t}" + (" not null" if n == "NO" else "") + (f" default {d}" if d else "") for c, t, n, d in cols]
        lines += [f"  constraint {n} {d}" for n, d in cons]
        return f"create table {name} (\n" + ",\n".join(lines) + "\n);"
    return ""


# --- by database --------------------------------------------------------------

def _pick(db):
    return {"oracle": (_oracle_kinds, _oracle_objects, _oracle_detail, _oracle_ddl),
            "postgres": (_pg_kinds, _pg_objects, _pg_detail, _pg_ddl),
            "sqlite": (_sqlite_kinds, _sqlite_objects, _sqlite_detail, _sqlite_ddl)}[db.kind]


def kinds(db) -> list[tuple[str, int]]:
    return _pick(db)[0](db._con)


def objects(db, kind: str) -> list[tuple[str, str]]:
    return _pick(db)[1](db._con, kind)


def detail(db, kind: str, name: str) -> list[Section]:
    return _pick(db)[2](db._con, kind, name)


def ddl(db, kind: str, name: str) -> str:
    return _pick(db)[3](db._con, kind, name)
