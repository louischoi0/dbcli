"""SQL formatting for the cell under the cursor (Ctrl-U), by sqlparse.

The statement is re-indented clause by clause (SELECT lists one item to a line, JOIN / WHERE /
AND / GROUP BY on lines of their own, subqueries indented), with spaces around operators.
Keywords and names keep the case you typed them in; comments and hints stay where they are.
PL/SQL blocks and template calls (`@name args`) are left alone: sqlparse would misread them.
"""
from __future__ import annotations

import sqlparse

from .db import PLSQL_RE


class NotFormatted(Exception):
    """Why a cell was left as it was."""


def format_sql(text: str) -> str:
    body = text.strip()
    if not body:
        raise NotFormatted("nothing to format")
    if body.startswith("@"):
        raise NotFormatted("a template call is not formatted")
    if PLSQL_RE.match(body):
        raise NotFormatted("PL/SQL blocks are not formatted")
    out = sqlparse.format(body, reindent=True, use_space_around_operators=True, indent_width=4,
                          strip_whitespace=False)
    return "\n".join(line.rstrip() for line in out.strip().splitlines())
