"""SQL call templates: `@name arg1 arg2` fills $1, $2 … of a stored query.

    [templates]
    a = "select * from t where num <= $1"

    @a 4        ->  select * from t where num <= 4

Arguments are inserted as written (text substitution, so the completed query is
visible). Quotes group an argument and are kept: @b 'kim lee' -> 'kim lee'.

With a file (.dbclidata/templates.json) the templates are kept there, each with an id, and
the Template panel tab adds, edits and deletes them; every terminal shares the file and picks
up another's changes. The config's [templates] only fill a file that does not exist yet.
"""
from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

CALL_RE = re.compile(r"\s*@([A-Za-z_][\w-]*)(?:\s+(.*?))?\s*;?\s*$", re.S)
NAME_PREFIX_RE = re.compile(r"\s*@([A-Za-z_][\w-]*)?$")  # "@", "@se" (no args yet)
PARAM_RE = re.compile(r"\$(\d+)")
NAME_RE = re.compile(r"[A-Za-z_][\w-]*")
# a call inside a line: `@` at the line's start or after a space, `(` or `,` (not `user@dblink`)
INLINE_AT_RE = re.compile(r"(?:^|(?<=[\s(,=]))@(?=[A-Za-z_]|$)")


@dataclass
class Template:
    id: int
    alias: str
    body: str


class TemplateError(Exception):
    pass


class Templates:
    def __init__(self, table: dict[str, str] | None = None, path: Path | None = None):
        self.path = path
        self._entries: list[Template] = []
        self._next_id = 1
        self._mtime: float | None = None
        if path is not None and path.exists():
            self._load()
        else:
            for alias, body in sorted((table or {}).items()):
                self._entries.append(Template(self._next_id, alias, body))
                self._next_id += 1
            if path is not None and self._entries:
                self._save()

    # -- the file (shared by every terminal) -------------------------------
    def _load(self) -> None:
        try:
            st = self.path.stat()
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return  # unreadable: keep what is loaded
        self._mtime = st.st_mtime
        self._entries = [Template(int(t["id"]), str(t["alias"]), str(t["body"]))
                         for t in data.get("templates", [])]
        self._next_id = max([int(data.get("next_id", 1))] + [t.id + 1 for t in self._entries])

    def _save(self) -> None:
        data = {"next_id": self._next_id,
                "templates": [{"id": t.id, "alias": t.alias, "body": t.body} for t in self._entries]}
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
        self.refresh()
        return {t.alias: t.body for t in self._entries}

    # -- add / edit / delete (the Template tab) ------------------------------
    def entries(self) -> list[Template]:
        self.refresh()
        return list(self._entries)

    def check_alias(self, alias: str, keep: int | None = None) -> str | None:
        """Why the alias cannot be used (None: it can); `keep` is the id being renamed."""
        if not NAME_RE.fullmatch(alias):
            return "an alias takes letters, digits, _ and - (and starts with a letter or _)"
        if any(t.alias == alias and t.id != keep for t in self.entries()):
            return f"@{alias} exists already"
        return None

    def add(self, alias: str, body: str) -> Template:
        err = self.check_alias(alias)
        if err:
            raise TemplateError(err)
        t = Template(self._next_id, alias, body)
        self._next_id += 1
        self._entries.append(t)
        self._save()
        return t

    def update(self, id: int, alias: str | None = None, body: str | None = None) -> None:
        self.refresh()
        t = next((t for t in self._entries if t.id == id), None)
        if t is None:
            raise TemplateError(f"no template #{id}")
        if alias is not None:
            err = self.check_alias(alias, keep=id)
            if err:
                raise TemplateError(err)
            t.alias = alias
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
        return [n for n in sorted(self._table) if n.startswith(prefix)]

    def arity(self, name: str) -> int:
        """Number of arguments a template needs: the highest $n it uses."""
        return max((int(n) for n in PARAM_RE.findall(self._table[name])), default=0)

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
        name, rest = m.group(1), m.group(2) or ""
        if name not in self._table:
            raise TemplateError(f"no template '@{name}'")
        try:
            args = shlex.split(rest, posix=False)
        except ValueError as e:  # e.g. unbalanced quote
            raise TemplateError(f"@{name}: cannot read the arguments ({e})") from None
        return name, args

    def expand(self, text: str) -> str:
        name, args = self.parse(text)
        need = self.arity(name)
        if len(args) != need:
            raise TemplateError(f"@{name}: needs {need} argument(s), got {len(args)} — "
                                f"{self.describe(name)}")
        return PARAM_RE.sub(lambda m: args[int(m.group(1)) - 1], self._table[name])

    def describe(self, name: str) -> str:
        return " ".join(self._table[name].split())

    def listing(self) -> str:
        if not self._table:
            return "-- no templates defined (config file, [templates])"
        width = max(len(n) for n in self._table)
        return "\n".join(f"@{n.ljust(width)} {self.describe(n)}" for n, _ in self.items())
