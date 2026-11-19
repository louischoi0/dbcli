"""Abbreviation expansion: typing `$name` and pressing Tab replaces it with a snippet."""
from __future__ import annotations

import re
from dataclasses import dataclass

# `$name` ending at the cursor. The look-behind keeps Oracle identifiers such as
# `v$session` or `dba_hist$x` from being treated as abbreviations.
TOKEN_RE = re.compile(r"(?<![\w$#])\$([A-Za-z0-9_]+)$")

DEFAULT_ABBREVIATIONS: dict[str, str] = {
    "a": "select * from ",
    "c": "select count(*) from ",
    "w": "where ",
    "o": "order by ",
    "g": "group by ",
}


@dataclass(frozen=True)
class Expansion:
    token: str  # text to delete before the cursor, e.g. "$a"
    body: str  # text inserted in its place


class Abbreviations:
    def __init__(self, table: dict[str, str] | None = None):
        self._table = dict(DEFAULT_ABBREVIATIONS if table is None else table)

    def match(self, text_before_cursor: str) -> Expansion | None:
        m = TOKEN_RE.search(text_before_cursor)
        if not m:
            return None
        body = self._table.get(m.group(1))
        return None if body is None else Expansion(m.group(0), body)

    def items(self) -> list[tuple[str, str]]:
        return sorted(self._table.items())
