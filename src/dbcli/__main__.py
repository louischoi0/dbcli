"""Usage: ./dbcli [--config FILE] [--list] [CONNECTION | SQLITE-FILE]

One terminal runs one connection. Connections, passwords included, come from
.dbclidata/config.toml (see config.example.toml); with no argument the one added last
(lowest in the file) opens. A path to a SQLite file opens that file instead.
Each connection keeps its own history, last result and trace in .dbclidata/sessions/<name>/.
"""
import argparse
import os
import sys
import time
from pathlib import Path

from .abbrev import Abbreviations
from .config import CONFIG_PATH, DATA_DIR, ConfigError, Profile, load
from .templates import Templates

SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")


def use_time_zone(name: str) -> None:
    """Show and record every time in this zone (history, run times): Korea time by default."""
    os.environ["TZ"] = name
    time.tzset()


def main() -> None:
    use_time_zone(os.environ.get("DBCLI_TZ", "Asia/Seoul"))
    ap = argparse.ArgumentParser(prog="dbcli", description="One terminal, one connection.")
    ap.add_argument("--config", type=Path, default=CONFIG_PATH)
    ap.add_argument("--list", action="store_true", help="list the connections and exit")
    ap.add_argument("target", nargs="?", help="a connection name from the config, or a SQLite file")
    args = ap.parse_args()
    try:
        cfg = load(args.config)
    except (ConfigError, ValueError) as e:  # tomllib errors are ValueErrors
        sys.exit(f"config error in {args.config}: {e}")
    if args.list:
        for i, p in enumerate(cfg.profiles):
            print(f"{p.name:<20} {p.type:<9} {p.target()}{'   (default)' if i == 0 else ''}")
        return
    names = {p.name: p for p in cfg.profiles}
    if args.target is None:
        profile = cfg.profiles[0] if cfg.profiles else Profile("memory", "sqlite", {"path": ":memory:"})
    elif args.target in names:
        profile = names[args.target]
    elif args.target.endswith(SQLITE_SUFFIXES) or "/" in args.target or Path(args.target).exists():
        profile = Profile(Path(args.target).name, "sqlite", {"path": args.target})
    else:
        known = ", ".join(names) or "none"
        sys.exit(f"no connection named {args.target!r} (connections: {known}). "
                 "A SQLite file needs a path or a .db/.sqlite suffix.")

    from .ui import run  # imported late so a missing dependency gives a readable error

    DATA_DIR.mkdir(mode=0o700, exist_ok=True)
    # kept in templates.json (the Template tab edits them); the config's [templates] fill it once
    templates = Templates(cfg.templates, path=DATA_DIR / "templates.json")
    run([profile], Abbreviations(cfg.abbreviations), cfg.ui, templates)


if __name__ == "__main__":
    try:
        main()
    except ModuleNotFoundError as e:
        sys.exit(f"{e}. Install dependencies first: .venv/bin/pip install -r requirements.txt")
