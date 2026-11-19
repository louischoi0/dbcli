"""`./dbcli subscription list | add | set | delete | show | unset`: the AI tab's Anthropic API
keys, each a named subscription in config.toml:

    [subscriptions.work]
    api_key = "sk-ant-…"
    model = "claude-opus-5-5"     # optional, else [ai] model
    [ai]
    subscription = "work"         # the active one (`set NAME`)

The file is edited as text, as `./dbcli connection` does: a subscription is one block, and
[ai] changes one `key = value` line at a time; the result must parse before it is saved, and
the file stays owner-only. A key is never printed in full.
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from pathlib import Path

from .config import AI_EFFORTS, CONFIG_PATH, ConfigError, load
from .connections import (HEADER_RE, NAME_RE, NEW_FILE, ConnectionError_, _read, _write,
                          join_blocks, split_blocks, toml_value)

SECTION = "ai"


def _is_header(line: str) -> str | None:
    m = HEADER_RE.match(line)
    return m.group(1).strip() if m and not line.lstrip().startswith("#") else None


def _section(lines: list[str]) -> tuple[int, int] | None:
    """[start, end) of the [ai] section's lines (start is its header), or None."""
    start = next((i for i, l in enumerate(lines) if _is_header(l) == SECTION), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if _is_header(lines[i]) is not None), len(lines))
    return start, end


def _key_line(lines: list[str], start: int, end: int, key: str) -> int | None:
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=")
    return next((i for i in range(start + 1, end) if pat.match(lines[i])), None)


def set_value(text: str, key: str, value) -> str:
    """`key = value` under [ai]: replaced where it is, else added after the section's last
    setting, else a new [ai] section at the end of the file."""
    lines = text.splitlines(keepends=True) if text else [NEW_FILE]
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    new = f"{key} = {toml_value(value)}\n"
    sec = _section(lines)
    if sec is None:
        if lines and lines[-1].strip():
            lines.append("\n")
        return "".join(lines + [f"[{SECTION}]\n", new])
    start, end = sec
    at = _key_line(lines, start, end, key)
    if at is not None:
        lines[at] = new
    else:
        last = start
        for i in range(start + 1, end):  # after the last setting, before blanks / comments of the next section
            if lines[i].strip() and not lines[i].lstrip().startswith("#"):
                last = i
        lines.insert(last + 1, new)
    return "".join(lines)


def unset_value(text: str, key: str) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    sec = _section(lines)
    at = _key_line(lines, *sec, key) if sec else None
    if at is None:
        return text, False
    del lines[at]
    return "".join(lines), True


def mask(key: str) -> str:
    return f"{key[:7]}…{key[-4:]}" if len(key) > 14 else "…" * 3


def _ask_key() -> str:
    try:
        key = getpass.getpass("Anthropic API key (not shown; from console.anthropic.com → API keys): ").strip()
    except EOFError:
        raise ConnectionError_("cancelled") from None
    if not key:
        raise ConnectionError_("no key given: nothing changed")
    if not key.startswith("sk-ant-"):
        print("  note: Anthropic API keys start with sk-ant-; saving it anyway")
    return key


def check_key(key: str | None, model: str) -> str:
    """Ask the API about the model with this key: one line on how it went (nothing is billed)."""
    try:
        import anthropic
    except ModuleNotFoundError:
        return "not checked: the Anthropic SDK is not installed (.venv/bin/pip install anthropic)"
    try:
        client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        client.models.retrieve(model)
    except anthropic.AuthenticationError:
        return "the key was refused: check it (or create a new one)"
    except anthropic.PermissionDeniedError:
        return f"the key works, but may not use {model}"
    except anthropic.NotFoundError:
        return f"the key works, but there is no model {model!r} ([ai] model)"
    except anthropic.APIConnectionError:
        return "not checked: cannot reach the Anthropic API (network?)"
    except anthropic.APIStatusError as e:
        return f"not checked: API error {e.status_code}"
    return f"the key works: {model} is available"


TABLE = "subscriptions"


def names(path: Path) -> list[str]:
    return [n for n, _ in split_blocks(_read(path), TABLE) if n is not None]


def add(path: Path, name: str, api_key: str, model: str | None = None, effort: str | None = None,
        activate: bool = False) -> None:
    if not NAME_RE.fullmatch(name):
        raise ConnectionError_("a subscription name takes letters, digits, _ and -")
    text = _read(path)
    pieces = split_blocks(text, TABLE) if text else [(None, [NEW_FILE])]
    if name in [n for n, _ in pieces]:
        raise ConnectionError_(f"a subscription named {name!r} exists already (delete it first)")
    opts = {"api_key": api_key, **({"model": model} if model else {}), **({"effort": effort} if effort else {})}
    block = [f"[{TABLE}.{name}]\n"] + [f"{k} = {toml_value(v)}\n" for k, v in opts.items()]
    text = join_blocks(pieces + [(name, block)])
    if activate:
        text = set_value(text, "subscription", name)
    _write(path, text)


def activate(path: Path, name: str) -> None:
    if name not in names(path):
        raise ConnectionError_(f"no subscription named {name!r} (./dbcli subscription list)")
    _write(path, set_value(_read(path), "subscription", name))


def delete(path: Path, name: str) -> bool:
    """Remove the block; True when it was the active one (then none is active)."""
    pieces = split_blocks(_read(path), TABLE)
    if name not in [n for n, _ in pieces]:
        raise ConnectionError_(f"no subscription named {name!r}")
    text = join_blocks([p for p in pieces if p[0] != name])
    was_active = load(path).ai.subscription == name
    if was_active:
        text, _ = unset_value(text, "subscription")
    _write(path, text)
    return was_active


def _ask(label: str, default: str = "") -> str:
    try:
        answer = input(f"{label}{f' [{default}]' if default else ''}: ").strip()
    except EOFError:
        raise ConnectionError_("cancelled") from None
    return answer or default


def cmd_list(args) -> None:
    ai = load(args.config).ai
    if not ai.subscriptions:
        print("no subscriptions yet: ./dbcli subscription add")
        if ai.api_key:
            print(f"([ai] api_key {mask(ai.api_key)} is used meanwhile)")
        return
    w = max(len(n) for n in ai.subscriptions)
    for name, sub in ai.subscriptions.items():
        mark = "*" if name == ai.subscription else " "
        extra = " · ".join(x for x in (sub.model, sub.effort) if x)
        print(f"{mark} {name:<{w}}  {mask(sub.api_key)}" + (f"  {extra}" if extra else ""))
    print("(* the active one: the AI tab uses it; ./dbcli subscription set NAME changes it)"
          if ai.subscription in ai.subscriptions else "(none is active: ./dbcli subscription set NAME)")


def cmd_add(args) -> None:
    existing = set(names(args.config))
    name = args.name
    while not name or not NAME_RE.fullmatch(name) or name in existing:
        if name and not NAME_RE.fullmatch(name):
            print("  a subscription name takes letters, digits, _ and -")
        elif name in existing:
            print(f"  {name!r} exists already")
        name = _ask("Subscription name", "" if "default" in existing else "default")
    key = _ask_key()
    first = load(args.config).ai.active is None
    add(args.config, name, key, args.model, args.effort, activate=first or args.use)
    print(f"added {name} ({mask(key)})" + (" · now the active subscription" if first or args.use else ""))
    if not args.no_check:
        ai = load(args.config).ai
        print(check_key(key, args.model or ai.model))


def cmd_set(args) -> None:
    if args.name:
        activate(args.config, args.name)
        print(f"{args.name} is the active subscription")
        return
    have = names(args.config)
    if not have:  # nothing to choose from yet: add the first one
        print("no subscriptions yet: adding one")
        args.name, args.use = None, True
        cmd_add(args)
        return
    for i, n in enumerate(have, 1):
        print(f"  {i}. {n}")
    pick = _ask("Use which (number or name)")
    name = have[int(pick) - 1] if pick.isdigit() and 1 <= int(pick) <= len(have) else pick
    activate(args.config, name)
    print(f"{name} is the active subscription")


def cmd_delete(args) -> None:
    if args.name not in names(args.config):
        raise ConnectionError_(f"no subscription named {args.name!r}")
    if not args.yes and not _ask(f"Delete subscription {args.name!r} (its key too)? (y/n)", "n").lower().startswith("y"):
        print("kept")
        return
    was_active = delete(args.config, args.name)
    print(f"deleted {args.name}" + (" · no subscription is active now (./dbcli subscription set NAME)" if was_active else ""))


def cmd_show(args) -> None:
    ai = load(args.config).ai
    eff = ai.effective()
    if ai.active:
        source = f"{mask(eff.api_key)} (subscription {ai.subscription})"
    elif ai.api_key:
        source = f"{mask(ai.api_key)} ([ai] api_key)"
    elif os.environ.get("ANTHROPIC_API_KEY"):
        source = f"{mask(os.environ['ANTHROPIC_API_KEY'])} (ANTHROPIC_API_KEY)"
    else:
        source = "none: ./dbcli subscription add (an `ant auth login` profile still works)"
    if ai.subscription and not ai.active:
        source += f" · [ai] subscription = {ai.subscription!r} is not defined"
    print(f"key     {source}\nmodel   {eff.model}\neffort  {eff.effort}")
    if args.check:
        print(check_key(eff.api_key, eff.model))


def cmd_unset(args) -> None:
    """No active subscription (and no [ai] api_key): the AI tab asks to pick one."""
    text = _read(args.config)
    text, a = unset_value(text, "subscription")
    text, b = unset_value(text, "api_key")
    if not (a or b):
        print("no subscription is active: nothing to change")
        return
    _write(args.config, text)
    print("no subscription is active now (the subscriptions themselves are kept)")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="./dbcli subscription",
                                 description="The AI tab's Anthropic API keys, as named subscriptions in config.toml.")
    ap.add_argument("--config", type=Path, default=CONFIG_PATH)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="the subscriptions; * marks the active one")
    a = sub.add_parser("add", help="add one: a name and its API key (asked without echo)")
    a.add_argument("name", nargs="?")
    a.add_argument("--model", help="this subscription's model, e.g. claude-sonnet-5-5 (else [ai] model)")
    a.add_argument("--effort", choices=AI_EFFORTS)
    a.add_argument("--use", action="store_true", help="make it the active one (the first one always is)")
    a.add_argument("--no-check", action="store_true", help="do not check the key with the API")
    s = sub.add_parser("set", help="make one the active subscription (asks which, or adds the first)")
    s.add_argument("name", nargs="?")
    s.add_argument("--model", help=argparse.SUPPRESS)
    s.add_argument("--effort", choices=AI_EFFORTS, help=argparse.SUPPRESS)
    s.add_argument("--no-check", action="store_true", help=argparse.SUPPRESS)
    d = sub.add_parser("delete", help="delete one")
    d.add_argument("name")
    d.add_argument("-y", "--yes", action="store_true", help="do not ask")
    sh = sub.add_parser("show", help="the active key (masked), its source, model and effort")
    sh.add_argument("--check", action="store_true", help="also check the key with the API")
    sub.add_parser("unset", help="make none active (the AI tab then asks to pick one)")
    args = ap.parse_args(argv)
    try:
        {"list": cmd_list, "add": cmd_add, "set": cmd_set, "delete": cmd_delete,
         "show": cmd_show, "unset": cmd_unset}[args.cmd](args)
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
