"""Which names in a statement are tables, views and columns, to check they exist.

`table_refs(sql)` finds the names after FROM (and its comma list), JOIN, INTO and UPDATE,
with their offsets. It skips what it cannot judge: schema-qualified names (`hr.emp`),
database links (`emp@remote`), quoted names, functions (`table(...)`), subqueries, the
names a WITH clause defines, `FROM` inside a function call (`extract(year from d)`,
`trim(x from s)`) and `FOR UPDATE`.

`column_refs(sql)` finds the column names of a query or DML statement, each with the table
it must belong to: `alias.col` names its table, a bare `col` must be in one of the
statement's tables. A bare name is only judged when every table of the statement could be
(no subquery, table function, CTE, qualified name or link in a FROM), and keywords,
functions, aliases, pseudo-columns and bind variables are never columns.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# comments and string literals become blanks of the same length, so offsets still match
NOISE_RE = re.compile(r"'(?:[^']|'')*'?|--[^\n]*|/\*.*?(?:\*/|\Z)", re.S)
LEAD_RE = re.compile(r"\b(from|join|into|update)\b", re.I)
NAME_RE = re.compile(r'"[^"]*"|[^\W\d][\w$#]*(?:\s*\.\s*[^\W\d"][\w$#"]*)?(?:@[\w.$#]+)?')
CTE_RE = re.compile(r"(?:\bwith\b|,)\s*([^\W\d][\w$#]*)\s*(?:\([^)]*\)\s*)?as\s*\(", re.I)
SPACE_RE = re.compile(r"\s*")
ALIAS_RE = re.compile(r"\s+(?:as\s+)?([^\W\d][\w$#]*)", re.I)
# words that end a table reference instead of being its alias
STOP = frozenset("""
where join on inner left right full outer cross natural using group order having union
minus intersect except connect start set values select fetch offset for partition pivot
unpivot model window returning lateral sample as of versions with limit when then
nowait skip wait
""".split())
SUBQUERY_RE = re.compile(r"\(\s*(select|with)\b", re.I)

# --- columns --------------------------------------------------------------
CHECKED_RE = re.compile(r"\s*(select|with|insert|update|delete|merge)\b", re.I)
# constructs whose names are too free to judge: the whole statement is left alone
OPAQUE_RE = re.compile(r"\b(pivot|unpivot|model|match_recognize|json_table|xmltable|lateral|"
                       r"apply|connect_by_root|returning)\b", re.I)
WORD_RE = re.compile(r"(?<![\w$#.:@])([^\W\d][\w$#]*)(?:\s*\.\s*([^\W\d][\w$#]*))?(?![\w$#])")
SQL_WORDS = frozenset("""
select from where and or not in is null like between exists case when then else end as on
join inner left right full outer cross natural using group by order having union all minus
intersect except distinct unique asc desc nulls first last fetch next rows row only offset
percent ties with insert into values update set delete merge matched create table drop alter
rename truncate commit rollback over partition range unbounded preceding following current
prior connect start nocycle escape any some date timestamp interval year month day hour
minute second zone local at both leading trailing for nowait skip locked wait of siblings
keep dense_rank within listagg overflow default sample seed versions period true false
returning lateral sysdate systimestamp current_date current_timestamp localtimestamp
dbtimezone sessiontimezone user uid rownum rowid level ora_rowscn nextval currval
connect_by_isleaf connect_by_iscycle collate character varying char varchar varchar2 number
integer int float double precision decimal numeric clob blob nclob raw binary_float
binary_double boolean multiset member submultiset separator limit ilike similar to
""".split())


def blank_noise(sql: str) -> str:
    return NOISE_RE.sub(lambda m: " " * len(m.group(0)), sql)


def blank_keep_quotes(sql: str) -> str:
    """As blank_noise, but a string keeps its quotes ('    '), so what follows it can be told
    apart: `'a' two` is a string and its alias."""
    def keep(m):
        t = m.group(0)
        if t.startswith("'") and len(t) >= 2 and t.endswith("'"):
            return "'" + " " * (len(t) - 2) + "'"
        return " " * len(t)
    return NOISE_RE.sub(keep, sql)


def _in_function_call(text: str, at: int) -> bool:
    """True when `at` is inside parentheses that are a call, not a subquery."""
    return _calls_at(text, [at])[0]


def _calls_at(text: str, positions: list[int]) -> list[bool]:
    """_in_function_call for each of the (ascending) positions, in one pass over the text
    (a pass per position made a long cell, with a FROM on every few lines, slow)."""
    out, stack, i = [], [], 0
    for at in positions:
        for j in range(i, at):
            ch = text[j]
            if ch == "(":
                stack.append(j)
            elif ch == ")" and stack:
                stack.pop()
        i = max(i, at)
        out.append(bool(stack) and not SUBQUERY_RE.match(text, stack[-1]))
    return out


def _after_for(text: str, at: int) -> bool:
    """`for` right before `at`, blanks between (SELECT … FOR UPDATE), read backwards."""
    j = at
    while j > 0 and text[j - 1].isspace():
        j -= 1
    return text[max(0, j - 3):j].lower() == "for" and (j < 4 or not (text[j - 4].isalnum() or text[j - 4] in "_$#"))


@dataclass
class Scan:
    tables: list[tuple[int, str, str | None]] = field(default_factory=list)  # (offset, name, alias)
    names: set[str] = field(default_factory=set)  # every name and alias in a table position
    opaque: bool = False  # some FROM item could not be judged
    ctes: set[str] = field(default_factory=set)


def scan(sql: str, text: str | None = None) -> Scan:
    """The statement's table references, their aliases, and whether all of them were judged."""
    text = blank_noise(sql) if text is None else text
    out = Scan(ctes={m.group(1).lower() for m in CTE_RE.finditer(text)})
    leads = list(LEAD_RE.finditer(text))
    in_call = _calls_at(text, [lead.start() for lead in leads])
    for lead, called in zip(leads, in_call):
        keyword = lead.group(1).lower()
        if keyword == "update" and _after_for(text, lead.start()):
            continue  # SELECT ... FOR UPDATE
        if keyword == "from" and called:
            continue  # extract(year from d), trim(x from s)
        pos, listing = lead.end(), keyword == "from"
        while True:
            pos = SPACE_RE.match(text, pos).end()
            if pos >= len(text):
                break
            if text[pos] == "(":
                out.opaque = True  # a subquery as a table
                break
            name = NAME_RE.match(text, pos)
            if not name:
                break
            word = name.group(0)
            if word.lower() in STOP:
                break
            after = SPACE_RE.match(text, name.end()).end()
            is_call = after < len(text) and text[after] == "("
            plain = not (word.startswith('"') or "." in word or "@" in word)
            if plain and not is_call and word.lower() not in out.ctes:
                entry = [name.start(), word, None]
                out.tables.append(entry)
            else:
                out.opaque, entry = True, None
            out.names.add(re.split(r"[.@]", word.strip('"'))[-1].lower())
            pos = name.end()
            if is_call:  # skip the call's parentheses
                depth, pos = 0, after
                while pos < len(text):
                    depth += {"(": 1, ")": -1}.get(text[pos], 0)
                    pos += 1
                    if depth == 0:
                        break
            alias = ALIAS_RE.match(text, pos)
            if alias and alias.group(1).lower() not in STOP:
                out.names.add(alias.group(1).lower())
                if entry is not None:
                    entry[2] = alias.group(1)
                pos = alias.end()
            comma = re.compile(r"\s*,").match(text, pos)
            if not (listing and comma):
                break
            pos = comma.end()
    out.tables = [tuple(t) for t in out.tables]
    return out


TABLE_SLOT_RE = re.compile(
    r"\b(?:(from)\s+(?:[^\W\d][\w$#.@]*(?:\s+(?:as\s+)?[^\W\d][\w$#]*)?\s*,\s*)*"
    r"|(?:join|into|update)\s+)(?:[^\W\d][\w$#]*)?$", re.I)


def in_table_slot(before: str) -> bool:
    """True when the word being typed at the end of `before` stands where a table goes:
    right after FROM, JOIN, INTO or UPDATE, or after a comma in a FROM list."""
    text = blank_noise(before)
    m = TABLE_SLOT_RE.search(text)
    if not m:
        return False
    if re.search(r"\bfor\s+update\s+\S*$", text, re.I):
        return False
    return not (m.group(1) and _in_function_call(text, m.start()))


def table_refs(sql: str) -> list[tuple[int, str]]:
    """(offset, name) of each plain table or view name the statement uses."""
    return [(at, name) for at, name, _ in scan(sql).tables]


# keywords that end an expression (a value or CASE … END), so a name after them is an alias
VALUE_WORDS = frozenset("""
end null true false sysdate systimestamp current_date current_timestamp localtimestamp
rownum rowid level user uid dbtimezone sessiontimezone
""".split())
QUOTED_NAME_RE = re.compile(r'"[^"]*"?')


def _select_aliases(text: str) -> set[str]:
    """Names a select list gives its expressions: `expr as x` and `expr x`."""
    out = {m.group(1).lower() for m in re.finditer(r"\bas\s+([^\W\d][\w$#]*)", text, re.I)}
    # an implicit alias: a name right after an expression's end, then `,` or FROM
    for m in re.finditer(r"([\w$#]+|[)'\]])\s+([^\W\d][\w$#]*)\s*(?=,|\bfrom\b)", text, re.I):
        before, name = m.group(1).lower(), m.group(2).lower()
        if (before not in SQL_WORDS or before in VALUE_WORDS) and name not in SQL_WORDS:
            out.add(name)
    return out


@dataclass
class ColumnRef:
    at: int  # offset of the column name
    name: str
    table: str | None  # the table it must be in; None: any of the statement's tables


def column_refs(sql: str) -> tuple[list[ColumnRef], list[str]] | None:
    """The statement's column names to check, and the tables they are checked against.

    None when the statement is not one to judge (DDL, PL/SQL, PIVOT and the like).
    """
    if not CHECKED_RE.match(sql) or OPAQUE_RE.search(blank_noise(sql)):
        return None
    # quoted names ("Total", "MyCol") are never judged: blanked like strings
    text = QUOTED_NAME_RE.sub(lambda m: " " * len(m.group(0)), blank_noise(sql))
    s = scan(sql, blank_noise(sql))
    by_alias = {}
    for _, table, alias in s.tables:
        by_alias[table.lower()] = table
        if alias:
            by_alias[alias.lower()] = table
    tables = sorted({t for _, t, _ in s.tables}, key=str.lower)
    if not tables:
        return None
    skip = SQL_WORDS | s.names | s.ctes | _select_aliases(QUOTED_NAME_RE.sub(
        lambda m: " " * len(m.group(0)), blank_keep_quotes(sql)))
    table_spans = {(at, at + len(name)) for at, name, _ in s.tables}
    refs: list[ColumnRef] = []
    for m in WORD_RE.finditer(text):
        first, second = m.group(1), m.group(2)
        after = SPACE_RE.match(text, m.end()).end()
        if after < len(text) and text[after] == "(":
            continue  # a function or procedure call
        if any(a <= m.start() < b for a, b in table_spans):
            continue
        if second is not None:  # qualifier.column
            table = by_alias.get(first.lower())
            if table is not None and second.lower() not in SQL_WORDS:
                refs.append(ColumnRef(m.start(2), second, table))
            continue
        if s.opaque or first.lower() in skip or text[m.end():m.end() + 2] == "=>":
            continue
        refs.append(ColumnRef(m.start(1), first, None))
    return refs, tables
