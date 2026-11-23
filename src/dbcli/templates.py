"""The Archive tab's saved queries: bookmarks, templates and macros.

A **bookmark** is a saved query with an optional memo. A **template** has an `@alias` and is
called as `@alias arg1 arg2`: the query it stands for is shown (above the result, or put in
the text by Tab). A **macro** has a name and is called as `(name arg1 arg2)`, alone in a cell:
the stored query runs, the cell keeps showing the call.

In a template's or a macro's body the arguments go in two ways:

    $1   the argument's text, put in the query as written (a literal):
             select * from t where num <= $1        @a 4  ->  … num <= 4
    :1   a bind variable, bound to the argument (Oracle binds it):
             select * from emp where deptno = :1    (by_dept 10)  ->  :1 bound to '10'

Quotes group an argument and are kept for `$n`: @b 'kim lee' -> 'kim lee'; for `:n` they are
taken off (the value bound is kim lee). Other binds of the body (`:cust_id`) are the cell's,
as for any SQL.

With a file (.dbclidata/bookmarks.json) they are kept there, each with an id and its kind;
every terminal shares the file and picks up another's changes. Entries from before kinds were
kept: one with an alias is a template, the others bookmarks. A templates.json from before
bookmarks fills it once, and the config's [templates] fill a file that does not exist yet.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

CALL_RE = re.compile(r"\s*@([A-Za-z_][\w-]*)(?:\s+(.*?))?\s*;?\s*$", re.S)
NAME_PREFIX_RE = re.compile(r"\s*@([A-Za-z_][\w-]*)?$")  # "@", "@se" (no args yet)
# a macro call, the whole cell: `(name arg1 arg2)`
# a macro's name may start with `*` (`(*cnt 주문)`) and take letters of any script
MACRO_RE = re.compile(r"\s*\(\s*(\*?[^\W\d][\w-]*)(?:\s+(.*?))?\s*\)\s*;?\s*$", re.S)
PARAM_RE = re.compile(r"\$(\d+)")
NAME_RE = re.compile(r"[A-Za-z_][\w-]*")  # a template's @name
MACRO_NAME_RE = re.compile(r"\*?[^\W\d][\w-]*")  # a macro's: `*cnt`, `주문수` too
# a call inside a line: `@` at the line's start or after a space, `(` or `,` (not `user@dblink`)
INLINE_AT_RE = re.compile(r"(?:^|(?<=[\s(,=]))@(?=[A-Za-z_]|$)")
KINDS = ("bookmark", "template", "macro")  # the Archive tab's pages, in this order


@dataclass
class Template:
    """An Archive entry: a query with an optional memo; a template (`@alias`) or a macro
    (`(alias)`) has a name, kept in `alias`."""
    id: int
    alias: str | None
    body: str
    memo: str = ""
    kind: str = "bookmark"  # bookmark | template | macro

    @property
    def call(self) -> str:
        """How it is called: `@name`, `(name …)`, or "" for a bookmark."""
        if self.kind == "template" and self.alias:
            return f"@{self.alias}"
        if self.kind == "macro" and self.alias:
            return f"({self.alias})"
        return ""

    @property
    def label(self) -> str:
        """How messages name it: its memo, else its call, else #id."""
        return self.memo or self.call or f"#{self.id}"


def _named(table: dict[str, str], name: str) -> str | None:
    """The name as `table` keeps it, matched regardless of case (`*TBL` is `*tbl`: SQL is
    often typed in capitals, and Tab may put them in); None when there is none."""
    if name in table:
        return name
    folded = name.casefold()
    return next((k for k in table if k.casefold() == folded), None)


def _numbered_binds(body: str) -> list[int]:
    """The `:1`, `:2` … bind variables of a body (not in strings or comments)."""
    from .statements import bind_names
    return [int(n) for n in bind_names(body) if n.isdigit()]


def _arguments(rest: str, where: str) -> list[str]:
    """The call's arguments, split at blanks; a quoted part keeps its blanks and its quotes,
    and SQL's doubled quote stays inside it ('it''s' is one argument)."""
    out, i, n = [], 0, len(rest)
    while i < n:
        if rest[i].isspace():
            i += 1
            continue
        j = i
        while j < n and not rest[j].isspace():
            if rest[j] in "'\"":
                quote, j = rest[j], j + 1
                while True:
                    if j >= n:
                        raise TemplateError(f"{where}: cannot read the arguments (no closing quotation)")
                    if rest[j] == quote:
                        if quote == "'" and j + 1 < n and rest[j + 1] == "'":
                            j += 2  # '' inside a string
                            continue
                        j += 1
                        break
                    j += 1
            else:
                j += 1
        out.append(rest[i:j])
        i = j
    return out


def _unquoted(arg: str) -> str:
    """An argument as a bind value: its quotes off ('kim lee' -> kim lee, 'it''s' -> it's)."""
    if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in "'\"":
        inner = arg[1:-1]
        return inner.replace("''", "'") if arg[0] == "'" else inner
    return arg


def fill(body: str, args: list[str], where: str) -> tuple[str, dict[str, str]]:
    """The body with its arguments: `$n` replaced by the argument as written, and the values
    of its `:n` bind variables (argument n, unquoted) by name ("1", "2" …)."""
    need = max([int(n) for n in PARAM_RE.findall(body)] + _numbered_binds(body), default=0)
    if len(args) != need:
        raise TemplateError(f"{where}: needs {need} argument(s), got {len(args)} — {' '.join(body.split())}")
    sql = PARAM_RE.sub(lambda m: args[int(m.group(1)) - 1], body)
    return sql, {str(n): _unquoted(args[n - 1]) for n in _numbered_binds(body)}


KEEP = object()  # update(): leave the alias as it is (None clears it)


class TemplateError(Exception):
    pass


class Templates:
    def __init__(self, table: dict[str, str] | None = None, path: Path | None = None,
                 legacy: Path | None = None):
        self.path = path
        self._entries: list[Template] = []
        self._next_id = 1
        self._mtime: float | None = None
        if path is not None and path.exists():
            self._load()
        elif path is not None and legacy is not None and legacy.exists():
            self._load(legacy)  # the templates of before: each a bookmark named by its alias
            self._save()
        else:
            for alias, body in sorted((table or {}).items()):
                self._entries.append(Template(self._next_id, alias, body, kind="template"))
                self._next_id += 1
            if path is not None and self._entries:
                self._save()

    # -- the file (shared by every terminal) -------------------------------
    def _load(self, path: Path | None = None) -> None:
        path = path or self.path
        try:
            st = path.stat()
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return  # unreadable: keep what is loaded
        if path == self.path:
            self._mtime = st.st_mtime
        entries = []
        for t in data.get("bookmarks", data.get("templates", [])):
            alias = t.get("alias") or None
            # "name" was the memo's first form, filled with the alias when templates became bookmarks
            memo = t.get("memo", t.get("name") if t.get("name") != alias else "") or ""
            # before kinds: a bookmark with an alias was a template
            kind = t.get("kind") if t.get("kind") in KINDS else ("template" if alias else "bookmark")
            entries.append(Template(int(t["id"]), str(alias) if alias else None, str(t["body"]), str(memo), kind))
        self._entries = entries
        self._next_id = max([int(data.get("next_id", 1))] + [t.id + 1 for t in self._entries])

    def _save(self) -> None:
        data = {"next_id": self._next_id,
                "bookmarks": [{"id": t.id, "kind": t.kind, "memo": t.memo, "alias": t.alias, "body": t.body}
                              for t in self._entries]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        self._mtime = self.path.stat().st_mtime

    def refresh(self) -> None:
        """Take in what another terminal saved since."""
        if self.path is None:
            return
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return
        if mtime != self._mtime:
            self._load()

    @property
    def _table(self) -> dict[str, str]:
        """The templates: @alias -> body."""
        self.refresh()
        return {t.alias: t.body for t in self._entries if t.alias and t.kind == "template"}

    @property
    def _macros(self) -> dict[str, str]:
        """The macros: name -> body."""
        self.refresh()
        return {t.alias: t.body for t in self._entries if t.alias and t.kind == "macro"}

    # -- add / edit / delete (the Archive tab) ------------------------------
    def entries(self, kind: str | None = None) -> list[Template]:
        """Every entry, or those of one kind (an Archive page)."""
        self.refresh()
        return [t for t in self._entries if kind is None or t.kind == kind]

    def check_alias(self, alias: str | None, keep: int | None = None, kind: str = "template") -> str | None:
        """Why the name cannot be used by a `kind` (None: it can, also no name at all); `keep`
        is the id being renamed. Templates and macros have names of their own each."""
        if not alias:
            return None
        if kind == "macro":
            if not MACRO_NAME_RE.fullmatch(alias):
                return "a macro name takes letters, digits, _ and - (and starts with a letter, _ or *)"
        elif not NAME_RE.fullmatch(alias):
            return "a name takes letters, digits, _ and - (and starts with a letter or _)"
        if kind == "bookmark":
            return None
        if any(t.alias and t.alias.casefold() == alias.casefold() and t.kind == kind and t.id != keep
               for t in self.entries()):
            return f"{'@' + alias if kind == 'template' else '(' + alias + ')'} exists already"
        return None

    def add(self, body: str, memo: str = "", alias: str | None = None, kind: str | None = None) -> Template:
        """An entry; `kind` left out: a template when it has an alias, else a bookmark."""
        kind = kind or ("template" if alias else "bookmark")
        if kind != "bookmark" and not alias:
            raise TemplateError(f"a {kind} needs a name")
        err = self.check_alias(alias, kind=kind)
        if err:
            raise TemplateError(err)
        self.refresh()
        t = Template(self._next_id, alias or None, body, memo.strip(), kind)
        self._next_id += 1
        self._entries.append(t)
        self._save()
        return t

    def update(self, id: int, alias=KEEP, body: str | None = None, memo: str | None = None,
               kind: str | None = None) -> None:
        """Change an entry; `kind` moves it to another page (a template or a macro needs a name)."""
        self.refresh()
        t = next((t for t in self._entries if t.id == id), None)
        if t is None:
            raise TemplateError(f"no entry #{id}")
        new_kind = kind or t.kind
        new_alias = t.alias if alias is KEEP else (alias or None)
        if new_kind != "bookmark" and not new_alias:
            raise TemplateError(f"a {new_kind} needs a name")
        if alias is not KEEP or kind is not None:
            err = self.check_alias(new_alias, keep=id, kind=new_kind)
            if err:
                raise TemplateError(err)
        t.alias, t.kind = new_alias, new_kind
        if memo is not None:
            t.memo = memo.strip()
        if body is not None:
            t.body = body
        self._save()

    def remove(self, id: int) -> None:
        self.refresh()
        self._entries = [t for t in self._entries if t.id != id]
        self._save()

    def __bool__(self) -> bool:
        return bool(self._table)

    def items(self) -> list[tuple[str, str]]:
        return sorted(self._table.items())

    def names(self, prefix: str = "") -> list[str]:
        return [n for n in sorted(self._table) if n.casefold().startswith(prefix.casefold())]

    def arity(self, name: str) -> int:
        """Number of arguments a template needs: the highest $n or :n it uses."""
        body = self._table[_named(self._table, name) or name]
        return max([int(n) for n in PARAM_RE.findall(body)] + _numbered_binds(body), default=0)

    def call_at(self, before: str, after: str) -> str | None:
        """The `@name args…` (or `@na`) that ends at the cursor inside a line, from its `@`:
        `select * from (@a 4` -> "@a 4". None when the cursor is not in one."""
        if after[:1] not in ("", " ", "\t", "\n", ")", ",", ";"):
            return None
        line = before.rsplit("\n", 1)[-1]
        starts = [m.start() for m in INLINE_AT_RE.finditer(line)]
        if not starts:
            return None
        seg = line[starts[-1]:]
        if NAME_PREFIX_RE.fullmatch(seg) or CALL_RE.fullmatch(seg):
            return seg
        return None

    @staticmethod
    def is_call(text: str) -> bool:
        """Input that should be treated as a template call (or the bare `@` listing)."""
        return text.strip() == "@" or CALL_RE.fullmatch(text) is not None

    def parse(self, text: str) -> tuple[str, list[str]]:
        m = CALL_RE.fullmatch(text)
        if not m:
            raise TemplateError("template call: @name arg1 arg2 …")
        typed, rest = m.group(1), m.group(2) or ""
        name = _named(self._table, typed)
        if name is None:
            raise TemplateError(f"no template '@{typed}'")
        return name, _arguments(rest, f"@{name}")

    def expand(self, text: str) -> str:
        """The template call's query, `$n` filled in (its `:n` binds stay in the text)."""
        return self.expand_call(text)[0]

    def expand_call(self, text: str) -> tuple[str, dict[str, str]]:
        """The template call's query and the values of its `:n` binds."""
        name, args = self.parse(text)
        return fill(self._table[name], args, f"@{name}")

    # -- macros: `(name arg1 arg2)`, alone in a cell ---------------------------------
    def macro_call(self, text: str) -> tuple[str, list[str]] | None:
        """(name, arguments) when the whole text calls a macro; None when it does not (it is
        then SQL: `(select 1 from dual)` stays a query)."""
        m = MACRO_RE.fullmatch(text)
        name = _named(self._macros, m.group(1)) if m else None
        if name is None:
            return None
        return name, _arguments(m.group(2) or "", f"({name})")

    def expand_macro(self, text: str) -> tuple[str, dict[str, str]] | None:
        """The query a macro call runs and the values of its `:n` binds; None: not a call."""
        call = self.macro_call(text)
        if call is None:
            return None
        name, args = call
        return fill(self._macros[name], args, f"({name})")

    def macro_names(self, prefix: str = "") -> list[str]:
        return [n for n in sorted(self._macros) if n.casefold().startswith(prefix.casefold())]

    def describe(self, name: str) -> str:
        return " ".join(self._table[_named(self._table, name) or name].split())

    def listing(self) -> str:
        if not self._table:
            return "-- no templates: add one on the Archive tab's Template page (Alt-5, ] then a)"
        width = max(len(n) for n in self._table)
        return "\n".join(f"@{n.ljust(width)} {self.describe(n)}" for n, _ in self.items())
