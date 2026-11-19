"""The statement under the cursor in a cell that holds several, for F4's parallel run.

`statement_at(text, cursor)` splits the cell with sqlparse (strings, comments and PL/SQL
blocks are understood, so a `;` inside them does not split) and returns the complete
statement around the cursor: one ended by `;`, or the cell's only statement. A cursor in the
unfinished text after the last `;` (or between statements, on blanks) finds none.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import sqlparse

TRAILING_NOISE_RE = re.compile(r"(?:\s+|--[^\n]*|/\*.*?\*/)*\Z", re.S)
READ_ONLY_RE = re.compile(r"\s*(?:/\*.*?\*/\s*|--[^\n]*\n\s*)*(select|with|values|table)\b", re.I | re.S)
FOR_UPDATE_RE = re.compile(r"\bfor\s+update\b", re.I)


PLSQL_START_RE = re.compile(r"\s*(?:declare|begin|create\s+(?:or\s+replace\s+)?(?:editionable\s+|noneditionable\s+)?"
                            r"(?:procedure|function|package|trigger|type\s+body))\b", re.I)
OPEN_RE = re.compile(r"\bbegin\b|(?<!end )\bcase\b", re.I)
CLOSE_RE = re.compile(r"\bend\b(?!\s+(?:if|loop)\b)", re.I)


def _block_done(text: str) -> bool:
    """A PL/SQL block gathered so far is whole: its `end`s close every begin and case."""
    from .objects import blank_noise
    plain = blank_noise(text)
    ends = len(CLOSE_RE.findall(plain))
    return ends >= max(1, len(OPEN_RE.findall(plain))) and plain.rstrip().endswith(";")


@dataclass
class Statement:
    start: int  # offsets in the cell's text, the statement's own text without the blanks around it
    end: int
    text: str  # with its `;`


def split(text: str) -> list[Statement]:
    """The cell's statements; a PL/SQL block (which sqlparse cuts at each `;`) kept whole."""
    out, at, block = [], 0, False
    for piece in (str(p) for p in sqlparse.parse(text)):
        lead = len(piece) - len(piece.lstrip())
        body = piece.strip()
        if body:
            if block and out:  # still inside the block: this piece belongs to it
                last = out[-1]
                last.end = at + lead + len(body)
                last.text = text[last.start:last.end]
            else:
                out.append(Statement(at + lead, at + lead + len(body), body))
                block = bool(PLSQL_START_RE.match(body))
            if block and _block_done(out[-1].text):
                block = False
        at += len(piece)
    return out


def _ended(stmt: Statement) -> bool:
    body = TRAILING_NOISE_RE.sub("", stmt.text)
    return body.endswith(";") or body.endswith("\n/")


def statement_at(text: str, cursor: int) -> Statement | None:
    stmts = [s for s in split(text) if TRAILING_NOISE_RE.sub("", s.text)]
    if len(stmts) == 1 and not _ended(stmts[0]):
        return stmts[0]  # the cell is one statement, `;` or not
    for s in stmts:
        if s.start <= cursor <= s.end:
            return s if _ended(s) else None
    return None


def read_only(sql: str) -> bool:
    """A query: safe on a connection of its own, as it changes nothing and locks nothing."""
    from .objects import blank_noise
    plain = blank_noise(sql)
    # a WITH may also lead a DML (with … insert / update), and FOR UPDATE locks rows
    return (bool(READ_ONLY_RE.match(plain)) and not FOR_UPDATE_RE.search(plain)
            and not re.search(r"\b(insert|update|delete|merge)\b", plain, re.I))
