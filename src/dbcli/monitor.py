"""The monitor tab's queries: sessions, blocking and locks, top SQL, wait events.

`snapshot(db)` returns the sections to draw, each a small table (see schema.Section).
Oracle reads V$SESSION, V$LOCKED_OBJECT, V$SQL and V$SYSTEM_EVENT, which need SELECT grants
(the section says which when one is missing); PostgreSQL reads pg_stat_activity and pg_locks.
"""
from __future__ import annotations

from .db import BG_MARK
from .schema import Section, _section

# What dbcli runs by itself (its BG_MARK comment) is not shown: a session busy with it shows
# as idle, its SQL left out of Top SQL and of the blocking rows.
MARK_LIKE = "'" + BG_MARK.strip() + "%'"
BG_SQL_IDS = f"(select sql_id from v$sql where sql_text like {MARK_LIKE})"

ORACLE_SECTIONS = (
    ("Sessions", f"""
        select s.sid, s.serial#, s.username "user",
               case when s.sql_id in {BG_SQL_IDS} then 'inactive' else lower(s.status) end status,
               substr(nvl(s.module, s.program), 1, 24) program,
               case when s.sql_id in {BG_SQL_IDS} then 'idle'
                    when s.state = 'WAITING' then s.event else 'on cpu' end activity,
               case when s.state = 'WAITING' and s.sql_id not in {BG_SQL_IDS} then s.seconds_in_wait end wait_s,
               s.last_call_et idle_s,
               case when s.sql_id in {BG_SQL_IDS} then null else s.sql_id end sql_id
        from v$session s where s.type = 'USER'
        order by decode(s.status, 'ACTIVE', 0, 1), s.last_call_et
        fetch first 25 rows only""", "grant select on v_$session"),
    ("Blocking", f"""
        select w.sid waiter, w.blocking_session blocker, w.event, w.seconds_in_wait wait_s,
               case when w.sql_id in {BG_SQL_IDS} then null else w.sql_id end waiter_sql,
               b.username blocker_user,
               case when b.sql_id in {BG_SQL_IDS} then null else b.sql_id end blocker_sql
        from v$session w left join v$session b on b.sid = w.blocking_session
        where w.blocking_session is not null order by w.seconds_in_wait desc""", "grant select on v_$session"),
    ("Locked objects", """
        select lo.session_id sid, o.owner, o.object_name object, o.object_type type,
               decode(lo.locked_mode, 1, 'null', 2, 'row share', 3, 'row exclusive', 4, 'share',
                      5, 'share row excl', 6, 'exclusive', lo.locked_mode) mode_held
        from v$locked_object lo join all_objects o on o.object_id = lo.object_id
        order by lo.session_id""", "grant select on v_$locked_object"),
    ("Top SQL (by elapsed time)", f"""
        select sql_id, executions execs,
               round(elapsed_time / 1000 / greatest(executions, 1), 1) ms_per_exec,
               round(elapsed_time / 1e6, 1) total_s, buffer_gets gets, rows_processed "rows",
               substr(regexp_replace(sql_text, '\\s+', ' '), 1, 50) sql_text
        from v$sql where parsing_schema_name not in ('SYS', 'SYSTEM')
          and sql_text not like {MARK_LIKE}
        order by elapsed_time desc fetch first 10 rows only""", "grant select on v_$sql"),
    ("Wait events (since startup)", """
        select event, wait_class, total_waits waits, round(time_waited_micro / 1e6, 1) waited_s,
               round(time_waited_micro / 1000 / greatest(total_waits, 1), 2) avg_ms
        from v$system_event where wait_class <> 'Idle'
        order by time_waited_micro desc fetch first 8 rows only""", "grant select on v_$system_event"),
)

PG_SECTIONS = (
    ("Sessions", f"""
        select pid, usename as "user",
               case when query like {MARK_LIKE} then 'idle' else state end as state,
               left(coalesce(application_name, ''), 24) as program,
               case when query like {MARK_LIKE} then 'idle'
                    else coalesce(wait_event_type || ': ' || wait_event, 'on cpu') end as activity,
               case when query not like {MARK_LIKE} then extract(epoch from now() - query_start)::int end as query_s,
               case when query not like {MARK_LIKE} then left(query, 50) end as query
        from pg_stat_activity where backend_type = 'client backend'
        order by state = 'active' desc, query_start""", ""),
    ("Blocking", f"""
        select pid as waiter, pg_blocking_pids(pid) as blockers, wait_event,
               case when query not like {MARK_LIKE} then left(query, 50) end as query
        from pg_stat_activity where cardinality(pg_blocking_pids(pid)) > 0""", ""),
    ("Locks", """
        select l.pid, c.relname as object, l.mode, l.granted from pg_locks l
        join pg_class c on c.oid = l.relation where c.relname not like 'pg_%' order by l.pid""", ""),
)


def snapshot(db) -> list[Section]:
    if db.kind == "oracle":
        out = []
        for title, sql, grant in ORACLE_SECTIONS:
            sec = _section(db._con, title, sql)
            if sec.error and ("ORA-00942" in sec.error or "ORA-01031" in sec.error):
                sec.error += f"  ({grant} to <user>; as SYS)"
            out.append(sec)
        return out
    if db.kind == "postgres":
        return [_section(db._con, title, sql) for title, sql, _ in PG_SECTIONS]
    return [Section("Monitor", error="SQLite has no sessions or locks to watch")]
