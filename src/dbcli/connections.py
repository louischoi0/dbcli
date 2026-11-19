"""`./dbcli connection list|add|delete|set`: manage the [connections.*] of config.toml from
the terminal.

The file is edited as text, one `[connections.NAME]` block at a time, so comments and the
other sections stay as written; every change is parsed again before it is saved. The last
block in the file is the default connection, so `set NAME` moves its block to the end, and
`add` puts a new one there (it becomes the default; `--keep-default` adds it above instead).
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
from pathlib import Path

from .config import CONFIG_PATH, TYPES, ConfigError, Profile, load

NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
HEADER_RE = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")
DEFAULT_PORTS = {"postgres": 5432, "oracle": 1521}
NEW_FILE = ("# dbcli connections and settings (see MANUAL.md, section 7). Keep it private: chmod 600.\n"
            "# The last [connections.*] is the default connection at startup.\n")


class ConnectionError_(Exception):
    """A connection command that cannot be done (shown as the error, exit status 1)."""


# -- the file as blocks ------------------------------------------------------------------
def _connection_of(header: str, table: str = "connections") -> str | None:
    """`connections.name` (or `connections."name"`) -> name; None for any other table."""
    m = re.fullmatch(rf'{table}\s*\.\s*(?:"([^"]+)"|([A-Za-z0-9_-]+))', header)
    return (m.group(1) or m.group(2)) if m else None


def split_blocks(text: str, table: str = "connections") -> list[tuple[str | None, list[str]]]:
    """The file as (connection name or None, lines) pieces, in order: a connection's piece runs
    from its header to the next table header. Comment lines right above a header go with that
    header (they describe it) when a blank line sets them off from what is above; a comment
    right under a block's last key (`# autocommit = false`) stays with that block."""
    lines = text.splitlines(keepends=True)
    pieces: list[tuple[str | None, list[str]]] = [(None, [])]
    is_comment = lambda l: l.lstrip().startswith("#")
    for line in lines:
        m = HEADER_RE.match(line)
        if m and not is_comment(line):
            prev = pieces[-1][1]
            k = len(prev)
            while k and is_comment(prev[k - 1]):
                k -= 1  # the comments just above the header
            lead = []
            if k < len(prev) and k > 0 and not prev[k - 1].strip():
                lead, prev[k:] = prev[k:], []
            pieces.append((_connection_of(m.group(1), table), lead + [line]))
        else:
            pieces[-1][1].append(line)
    return [(n, ls) for n, ls in pieces if ls or n is not None]


def join_blocks(pieces: list[tuple[str | None, list[str]]]) -> str:
    out = ""
    for i, (_, ls) in enumerate(pieces):
        chunk = "".join(ls)
        if out and not out.endswith("\n"):
            out += "\n"
        opening = i == 1 and all(not l.strip() or l.lstrip().startswith("#") for l in pieces[0][1])
        if out and chunk.strip() and not out.endswith("\n\n") and not chunk.startswith("\n") and not opening:
            out += "\n"  # a blank line between sections (not under the file's opening comment)
        out += chunk
    return out.rstrip("\n") + "\n" if out.strip() else out  # no blank lines left at the end


def toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    return json.dumps(str(v), ensure_ascii=False)  # a TOML basic string: \" \\ \n \uXXXX escapes


def block_text(name: str, options: dict) -> list[str]:
    return [f"[connections.{name}]\n"] + [f"{k} = {toml_value(v)}\n" for k, v in options.items()]


# -- reading and writing config.toml ----------------------------------------------------
def _read(path: Path) -> str:
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""


def _write(path: Path, text: str) -> None:
    """Check the new text parses as a dbcli config, then replace the file atomically (0600)."""
    tmp = path.with_name(path.name + ".tmp")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(text)
    try:
        load(tmp)
    except (ConfigError, ValueError) as e:
        tmp.unlink()
        raise ConnectionError_(f"the change would break {path.name} ({e}); nothing was saved") from None
    os.replace(tmp, path)


def names(path: Path) -> list[str]:
    """The connections in file order (the last one is the default)."""
    return [n for n, _ in split_blocks(_read(path)) if n is not None]


def add(path: Path, name: str, type_: str, options: dict, make_default: bool = True) -> None:
    if not NAME_RE.fullmatch(name):
        raise ConnectionError_("a connection name takes letters, digits, _ and -")
    if type_ not in TYPES:
        raise ConnectionError_(f"type must be one of {', '.join(TYPES)}")
    text = _read(path)
    pieces = split_blocks(text) if text else [(None, [NEW_FILE])]
    if name in [n for n, _ in pieces]:
        raise ConnectionError_(f"a connection named {name!r} exists already (delete it first)")
    block = (name, block_text(name, {"type": type_, **options}))
    conns = [i for i, (n, _) in enumerate(pieces) if n is not None]
    if make_default or not conns:
        pieces.append(block)
    else:  # just above the current default, which stays the last one
        pieces.insert(conns[-1], block)
    _write(path, join_blocks(pieces))


def delete(path: Path, name: str) -> None:
    pieces = split_blocks(_read(path))
    if name not in [n for n, _ in pieces]:
        raise ConnectionError_(f"no connection named {name!r}")
    _write(path, join_blocks([p for p in pieces if p[0] != name]))


def set_default(path: Path, name: str) -> None:
    pieces = split_blocks(_read(path))
    found = [p for p in pieces if p[0] == name]
    if not found:
        raise ConnectionError_(f"no connection named {name!r}")
    _write(path, join_blocks([p for p in pieces if p[0] != name] + found))


# -- the commands ------------------------------------------------------------------------
def cmd_list(path: Path) -> None:
    profiles = load(path).profiles  # default first
    if not profiles:
        print(f"no connections yet: ./dbcli connection add   ({path})")
        return
    w = max(len(p.name) for p in profiles)
    for i, p in enumerate(profiles):
        mark = "*" if i == 0 else " "
        secret = " · password from $" + p.options["password_env"] if "password_env" in p.options else ""
        print(f"{mark} {p.name:<{w}}  {p.type:<8}  {p.target()}{secret}")
    print("(* the default connection: ./dbcli opens it; ./dbcli connection set NAME changes it)")


def _ask(label: str, default: str | int | None = None, required: bool = True) -> str:
    shown = f" [{default}]" if default not in (None, "") else ""
    while True:
        try:
            answer = input(f"{label}{shown}: ").strip()
        except EOFError:
            raise ConnectionError_("cancelled") from None
        if answer:
            return answer
        if default not in (None, ""):
            return str(default)
        if not required:
            return ""
        print(f"  {label.lower()} is needed")


def _ask_port(default: int, given: str | None) -> int:
    while True:
        value = given or _ask("Port", default)
        if value.isdigit() and 0 < int(value) < 65536:
            return int(value)
        print("  a port is a number from 1 to 65535")
        given = None


def _ask_password(args) -> dict:
    """`password = "…"` (asked without echo), or `password_env` with --password-env."""
    if args.password_env:
        return {"password_env": args.password_env}
    try:
        pw = getpass.getpass("Password (not shown; empty: none): ")
    except EOFError:
        raise ConnectionError_("cancelled") from None
    return {"password": pw} if pw else {}


def gather(args) -> tuple[str, str, dict]:
    """Name, type and options for `add`, from the flags given and questions for the rest."""
    existing = set(names(args.config))
    name = args.name
    while not name or not NAME_RE.fullmatch(name) or name in existing:
        if name and not NAME_RE.fullmatch(name):
            print("  a connection name takes letters, digits, _ and -")
        elif name in existing:
            print(f"  {name!r} exists already")
        name = _ask("Connection name")
    type_ = args.type
    while type_ not in TYPES:
        if type_:
            print(f"  type is one of {', '.join(TYPES)}")
        type_ = _ask(f"Type ({' / '.join(TYPES)})", "oracle").lower()
    if type_ == "sqlite":
        return name, type_, {"path": args.path or _ask("Database file (or :memory:)")}
    host = args.host or _ask("Host", "localhost")
    port = _ask_port(DEFAULT_PORTS[type_], args.port)
    if type_ == "oracle":
        service = args.service or _ask("Service name (e.g. FREEPDB1, ORCL)")
        user = args.user or _ask("Account (user)")
        return name, type_, {"user": user, "dsn": f"{host}:{port}/{service}", **_ask_password(args)}
    dbname = args.dbname or _ask("Database name")
    user = args.user or _ask("Account (user)")
    return name, type_, {"host": host, "port": port, "dbname": dbname, "user": user, **_ask_password(args)}


def _test(name: str, type_: str, options: dict) -> None:
    from .db import error_text, open_db  # the drivers load only for a test
    print(f"connecting to {Profile(name, type_, options).target()} …", flush=True)
    try:
        open_db(Profile(name, type_, options)).close()
    except Exception as e:  # any driver error: report, the connection is saved anyway
        print(f"  failed: {error_text(e)}")
        return
    print("  connected")


def cmd_add(args) -> None:
    name, type_, options = gather(args)
    add(args.config, name, type_, options, make_default=not args.keep_default)
    is_default = not args.keep_default or len(names(args.config)) == 1
    print(f"added {name} ({Profile(name, type_, options).target()})"
          + (" · now the default connection" if is_default else ""))
    if args.test or (not args.no_test and sys.stdin.isatty()
                     and _ask("Test the connection now? (y/n)", "y").lower().startswith("y")):
        _test(name, type_, options)


def _confirm(question: str) -> bool:
    return _ask(f"{question} (y/n)", "n").lower().startswith("y")


def cmd_delete(args) -> None:
    if args.name not in names(args.config):
        raise ConnectionError_(f"no connection named {args.name!r}")
    if not args.yes and not _confirm(f"Delete connection {args.name!r} (its password too)?"):
        print("kept")
        return
    delete(args.config, args.name)
    left = load(args.config).profiles
    print(f"deleted {args.name}" + (f" · the default is {left[0].name}" if left else " · no connections left"))


def cmd_set(args) -> None:
    set_default(args.config, args.name)
    print(f"{args.name} is the default connection")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="./dbcli connection",
                                 description="List, add, delete the connections, or set the default one.")
    ap.add_argument("--config", type=Path, default=CONFIG_PATH)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="the connections; * marks the default")
    a = sub.add_parser("add", help="add one, asking for what the flags do not give")
    a.add_argument("name", nargs="?")
    a.add_argument("--type", choices=TYPES)
    a.add_argument("--host")
    a.add_argument("--port")
    a.add_argument("--service", help="Oracle: the service name (dsn host:port/service)")
    a.add_argument("--dbname", help="PostgreSQL: the database")
    a.add_argument("--user", help="the account")
    a.add_argument("--path", help="SQLite: the database file")
    a.add_argument("--password-env", metavar="VAR", help="read the password from $VAR instead of storing it")
    a.add_argument("--keep-default", action="store_true", help="do not make it the default connection")
    a.add_argument("--test", action="store_true", help="connect once to check it")
    a.add_argument("--no-test", action="store_true", help="do not offer to test it")
    d = sub.add_parser("delete", help="delete one")
    d.add_argument("name")
    d.add_argument("-y", "--yes", action="store_true", help="do not ask")
    s = sub.add_parser("set", help="make one the default (./dbcli opens it)")
    s.add_argument("name")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "list":
            cmd_list(args.config)
        elif args.cmd == "add":
            cmd_add(args)
        elif args.cmd == "delete":
            cmd_delete(args)
        else:
            cmd_set(args)
    except ConnectionError_ as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (ConfigError, ValueError) as e:
        print(f"config error in {args.config}: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ncancelled", file=sys.stderr)
        return 1
    return 0
