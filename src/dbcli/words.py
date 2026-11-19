"""Frequency-weighted word model behind the gray inline suggestion and Tab.

score(word) = base weight (SQL keywords, functions, schema names) + number of times the user
actually used it. On top of that, statistics collected from every statement run:
  - recency: a word used lately ranks a little higher, fading over days;
  - sequence: how often the word followed the word before the cursor (`order` -> `by`,
    `from` -> the tables you query, `nvl(` …).
The statistics live in words.db, shared by every dbcli running; `refresh()` (called
periodically in the background) reloads what other terminals learned and recomputes them.
Ties go to the shorter word, then alphabetical order.
"""
from __future__ import annotations

import bisect
import math
import re
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator

from .oracle_hints import HINTS

# Oracle identifiers may contain $ and # (v$session, x#tab).
WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$#]*")
PARTIAL_RE = re.compile(r"(?<![\w$#])[A-Za-z_][A-Za-z0-9_$#]*$")
WORD_CHAR_RE = re.compile(r"[\w$#]")
# String literals, quoted identifiers and comments are not learned.
NOISE_RE = re.compile(r"'(?:[^']|'')*'|\"[^\"]*\"|--[^\n]*|/\*.*?\*/", re.S)
# Hints are counted apart, as pairs after this pseudo word (`full` the hint is not FULL OUTER)
HINT_PREV = "/*+"
HINT_BLOCK_RE = re.compile(r"/\*\+(.*?)(?:\*/|\Z)", re.S)
HINT_ORDER = {h: i for i, h in enumerate(HINTS)}

MIN_PREFIX = 2
RECENCY_POINTS = 3.0  # a word used just now
RECENCY_DAYS = 3.0  # … fading with this time constant
SEQUENCE_POINTS = 6.0  # per doubling of how often it followed the previous word
# a column of a table the statement uses outranks words from elsewhere
CONTEXT_BOOST = 40

KEYWORDS: dict[str, float] = {
    # most common first: these win short prefixes until usage says otherwise
    "select": 50, "from": 50, "where": 45, "and": 40, "order": 30, "by": 30,
    "group": 25, "insert": 25, "into": 25, "values": 25, "update": 25, "set": 20,
    "delete": 20, "join": 20, "left": 18, "inner": 15, "on": 15, "as": 15,
    "having": 12, "limit": 12, "distinct": 12, "count": 12, "create": 12,
    "table": 12, "commit": 10, "rollback": 8, "between": 8, "like": 8, "null": 8,
    "not": 8, "exists": 8, "union": 8, "case": 8, "when": 8, "then": 8, "else": 8,
    "end": 8, "with": 8, "desc": 8, "asc": 6, "alter": 6, "drop": 6, "index": 6,
    "view": 5, "primary": 5, "key": 5, "references": 4, "default": 4,
    "sequence": 4, "nextval": 4, "sysdate": 4, "fetch": 4, "first": 4, "rows": 4,
    "only": 4, "offset": 4, "returning": 3, "truncate": 3, "grant": 3,
    "explain": 3, "describe": 3, "session": 2,
    # more keywords, rarer in everyday queries
    "outer": 6, "full": 4, "cross": 4, "natural": 2, "using": 6, "all": 8, "minus": 4,
    "intersect": 3, "nulls": 4, "last": 3, "next": 3, "partition": 6, "over": 8, "range": 3,
    "unbounded": 3, "preceding": 3, "following": 2, "current": 3, "row": 4, "prior": 3,
    "connect": 4, "start": 4, "level": 4, "rownum": 6, "rowid": 3, "merge": 4, "matched": 3,
    "replace": 4, "constraint": 3, "foreign": 3, "unique": 4, "check": 3, "column": 4,
    "add": 4, "modify": 3, "rename": 3, "synonym": 2, "procedure": 3, "function": 3,
    "package": 2, "trigger": 2, "begin": 4, "declare": 3, "exception": 2, "savepoint": 2,
    "interval": 3, "timestamp": 4, "date": 6, "escape": 2, "any": 3, "some": 2,
    "dual": 10, "systimestamp": 3, "user": 4,
    # functions (Oracle and common SQL)
    "nvl": 10, "nvl2": 3, "coalesce": 6, "decode": 8, "nullif": 3, "greatest": 3, "least": 3,
    "to_char": 10, "to_date": 10, "to_number": 6, "to_timestamp": 4, "trunc": 8, "round": 6,
    "substr": 8, "instr": 6, "length": 6, "upper": 6, "lower": 6, "initcap": 2, "trim": 6,
    "ltrim": 3, "rtrim": 3, "lpad": 4, "rpad": 3, "concat": 3, "regexp_like": 4,
    "regexp_substr": 4, "regexp_replace": 4, "add_months": 4, "months_between": 3,
    "last_day": 3, "extract": 4, "sum": 10, "avg": 8, "min": 8, "max": 8,
    "listagg": 5, "row_number": 6, "rank": 4, "dense_rank": 4, "lag": 4, "lead": 4,
    "first_value": 2, "last_value": 2, "ratio_to_report": 2, "cast": 6, "abs": 3, "mod": 4,
    "ceil": 3, "floor": 3, "power": 2, "sqrt": 2, "sys_context": 3, "dbms_output": 3,
}


def previous_word(before: str, prefix: str) -> str | None:
    """The word before the one being typed (`select * from em` -> `from`), lower case."""
    head = before[:len(before) - len(prefix)] if prefix and before.endswith(prefix) else before
    words = WORD_RE.findall(NOISE_RE.sub(" ", head))
    return words[-1].lower() if words else None


def match_case(prefix: str, word: str) -> str:
    """Keywords follow the case the user is typing in (SEL -> SELECT, sel -> select)."""
    return word.upper() if prefix.isupper() else word.lower()


class WordModel:
    def __init__(self, path: Path | None = None):
        self._base: dict[str, float] = {}
        self._uses: dict[str, float] = {}
        self._form: dict[str, str] = {}  # display form of identifiers
        self._keys: list[str] = []  # sorted lowercase keys for prefix lookup
        self._path = path
        self._last: dict[str, float] = {}  # word -> when it was last used (epoch seconds)
        self._pairs: dict[str, dict[str, float]] = {}  # previous word -> word -> times it followed
        self._recency: dict[str, float] = {}  # computed by refresh()
        self._pending: dict = {"uses": {}, "last": {}, "pairs": {}}  # learned, not yet in words.db
        # () -> (bonus, only, extra), set by the UI and asked on every lookup so suggestions
        # follow the statement: `bonus` a set of words to rank first (or a key -> points
        # function), `only` the words allowed at all (after `alias.`), `extra` a sorted list of
        # words offered here although the model does not hold them (table names after FROM).
        self.context = None
        for kw, w in KEYWORDS.items():
            self._ensure(kw, kw)
            self._base[kw] = w
        if path is not None:
            self._load()

    # -- building -------------------------------------------------------
    def _ensure(self, key: str, form: str) -> None:
        if key not in self._base and key not in self._uses:
            bisect.insort(self._keys, key)
        if key not in KEYWORDS:
            self._form[key] = form

    def add_keywords(self, words, weight: float) -> None:
        """Register keyword-like words (hints): their case follows the typing, like SQL keywords."""
        for word in words:
            key = word.lower()
            self._ensure(key, key)
            self._base[key] = max(self._base.get(key, 0.0), weight)

    def add_base(self, word: str, weight: float) -> None:
        """Register a word known from context (e.g. a table name) without a use."""
        key = word.lower()
        self._ensure(key, word)
        self._base[key] = max(self._base.get(key, 0.0), weight)

    def learn(self, sql: str, now: float | None = None) -> None:
        """Count each word the user ran, when, and which word it followed."""
        now = time.time() if now is None else now
        prev = None
        for word in WORD_RE.findall(NOISE_RE.sub(" ", sql)):
            key = word.lower()
            self._ensure(key, word)
            self._uses[key] = self._uses.get(key, 0.0) + 1
            self._last[key] = now
            self._recency[key] = RECENCY_POINTS
            pend = self._pending
            pend["uses"][key] = pend["uses"].get(key, 0.0) + 1
            pend["last"][key] = now
            if prev is not None:
                self._pairs.setdefault(prev, {})
                self._pairs[prev][key] = self._pairs[prev].get(key, 0.0) + 1
                pend["pairs"][(prev, key)] = pend["pairs"].get((prev, key), 0.0) + 1
            prev = key
        for block in HINT_BLOCK_RE.finditer(sql):  # /*+ full(e) use_nl(d) */: full, use_nl
            body = re.sub(r"\([^()]*\)", " ", block.group(1))
            for word in WORD_RE.findall(body):
                key = word.lower()
                if key in HINT_ORDER:
                    self._pairs.setdefault(HINT_PREV, {})
                    self._pairs[HINT_PREV][key] = self._pairs[HINT_PREV].get(key, 0.0) + 1
                    pend = self._pending["pairs"]
                    pend[(HINT_PREV, key)] = pend.get((HINT_PREV, key), 0.0) + 1

    def hint_candidates(self, prefix: str) -> list[str]:
        """Every hint extending `prefix` (all of them for none), most used first, then in the
        list's order (the common ones lead it)."""
        p = prefix.lower()
        used = self._pairs.get(HINT_PREV, {})
        keys = [h for h in HINTS if h.startswith(p) and h != p]
        keys.sort(key=lambda h: (-used.get(h, 0.0), HINT_ORDER[h]))
        return keys

    def refresh(self, now: float | None = None) -> None:
        """The periodic recount: reload what every dbcli has learned, recompute recency."""
        now = time.time() if now is None else now
        if self._path is not None:
            self.save()
            self._load()
        self._recency = {k: RECENCY_POINTS * math.exp(-(now - t) / (RECENCY_DAYS * 86400))
                         for k, t in self._last.items()}

    def sequence(self, prev: str | None, key: str) -> float:
        """Points for `key` coming after `prev`, from how often it did."""
        if not prev:
            return 0.0
        n = self._pairs.get(prev.lower(), {}).get(key, 0.0)
        return SEQUENCE_POINTS * math.log2(1 + n) if n else 0.0

    def score(self, key: str) -> float:
        return self._base.get(key, 0.0) + self._uses.get(key, 0.0)

    # -- lookup ---------------------------------------------------------
    def _context(self):
        found = tuple(self.context()) if self.context else ()
        boost, only, extra = (found + (None, None, None))[:3]
        if boost is None:
            bonus = lambda k: 0.0
        elif callable(boost):
            bonus = boost
        else:
            words = frozenset(boost)
            bonus = lambda k: CONTEXT_BOOST if k in words else 0.0
        return bonus, (frozenset(only) if only is not None else None), extra or ()

    def ranked(self, key: str, bonus, prev: str | None = None) -> float:
        return self.score(key) + bonus(key) + self._recency.get(key, 0.0) + self.sequence(prev, key)

    def candidates(self, prefix: str, limit: int = 20, before: str = "") -> list[str]:
        """Words extending `prefix`, best first, cased for display.

        After `alias.` only that table's columns qualify; otherwise the columns of the
        statement's tables come first.
        """
        bonus, only, extra = self._context()
        p = prefix.lower()
        keys = set()
        for pool in (self._keys, extra):
            lo = bisect.bisect_left(pool, p)
            hi = bisect.bisect_left(pool, p + "\U0010ffff")
            keys.update(k for k in pool[lo:hi] if k != p and (only is None or k in only))
        prev = previous_word(before, prefix)
        ordered = sorted(keys, key=lambda k: (-self.ranked(k, bonus, prev), len(k), k))
        return [self._display(prefix, k) for k in ordered[:limit]]

    def _display(self, prefix: str, key: str) -> str:
        form = key if key in KEYWORDS else self._form.get(key, key)  # extra words: as given
        if form.islower() or form.isupper():  # case-insensitive name: follow typing
            return prefix + match_case(prefix, form)[len(prefix):]
        return form  # mixed case ("MyTable") is spelled exactly as stored, prefix included

    def suggest(self, before: str, after: str = "") -> tuple[str, str] | None:
        """(typed prefix, suggested word). The word may re-case the prefix: my -> MyTable."""
        if after and WORD_CHAR_RE.match(after):
            return None  # cursor is inside a word
        m = PARTIAL_RE.search(before)
        if not m or len(m.group(0)) < MIN_PREFIX:
            return None
        prefix = m.group(0)
        best = self.candidates(prefix, limit=1, before=before)
        if not best:
            return None
        # "select" is already a word: don't push "selectivity" unless it is used more.
        bonus, _, _ = self._context()
        prev = previous_word(before, prefix)
        if self.ranked(prefix.lower(), bonus, prev) >= self.ranked(best[0].lower(), bonus, prev):
            return None
        return prefix, best[0]

    def suggest_suffix(self, before: str, after: str = "") -> str:
        """Gray text to show after the cursor, or "" for none."""
        s = self.suggest(before, after)
        return s[1][len(s[0]):] if s else ""

    # -- persistence ----------------------------------------------------
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Commit on success and always close (sqlite3's own `with` only commits)."""
        assert self._path is not None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self._path, timeout=5)) as con, con:
            con.execute("create table if not exists words (word text primary key, form text, uses real)")
            cols = {r[1] for r in con.execute("pragma table_info(words)")}
            if "last" not in cols:  # words.db from before the statistics
                con.execute("alter table words add column last real")
            con.execute("create table if not exists pairs (prev text, word text, n real, "
                        "primary key (prev, word))")
            yield con

    def _load(self) -> None:
        with self._connect() as con:
            for key, form, uses, last in con.execute("select word, form, uses, last from words"):
                self._ensure(key, form)
                self._uses[key] = uses + self._pending["uses"].get(key, 0.0)
                if last:
                    self._last[key] = max(last, self._pending["last"].get(key, 0.0))
            pairs: dict[str, dict[str, float]] = {}
            for prev, word, n in con.execute("select prev, word, n from pairs"):
                pairs.setdefault(prev, {})[word] = n
            for (prev, word), n in self._pending["pairs"].items():
                pairs.setdefault(prev, {})[word] = pairs.get(prev, {}).get(word, 0.0) + n
            self._pairs = pairs

    def save(self) -> None:
        """Add what was learned since the last save to words.db. Counts are added, not written
        over, so several dbcli terminals sharing the file all keep theirs."""
        if self._path is None:
            return
        pend = self._pending
        words = [(k, self._form.get(k, k), n, pend["last"].get(k)) for k, n in pend["uses"].items()]
        pairs = [(a, b, n) for (a, b), n in pend["pairs"].items()]
        with self._connect() as con:
            con.executemany(
                "insert into words (word, form, uses, last) values (?, ?, ?, ?) "
                "on conflict(word) do update set form = excluded.form, uses = uses + excluded.uses, "
                "last = max(coalesce(last, 0), coalesce(excluded.last, 0))",
                words,
            )
            con.executemany(
                "insert into pairs values (?, ?, ?) on conflict(prev, word) do update set n = n + excluded.n",
                pairs,
            )
        self._pending = {"uses": {}, "last": {}, "pairs": {}}
