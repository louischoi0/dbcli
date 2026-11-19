"""Per-connection files: .dbclidata/sessions/<connection>/

    history     queries run on this connection (the history tab), newest last
    last.json   the last run: its text, shell output, result rows and autotrace
    schema.json the schema tab where it was left (kinds, objects, an object's details and DDL)
                and the catalog Ctrl-O collected, so both outlive the terminal
    ai.json     the AI tab's latest questions and answers (10), so the chat outlives the terminal
    snapshot.json  F1's snapshot of the whole session (shell cells, prompt and cursor, the
                result's fetched rows and place), brought back once by the next start

Each terminal runs one connection, so what one connection did never shows up in another's.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
from decimal import Decimal
from pathlib import Path

from .config import DATA_DIR

SESSIONS_DIR = DATA_DIR / "sessions"


def session_dir(name: str, root: Path | None = None) -> Path:
    """The connection's folder (owner-only, like the rest of .dbclidata), made on first use."""
    safe = re.sub(r"[^\w.-]", "_", name) or "_"
    base = root or SESSIONS_DIR
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    d = base / safe
    d.mkdir(mode=0o700, exist_ok=True)
    return d


def _encode(v):
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else str(v)
    if isinstance(v, Decimal):
        return float(v) if v.is_finite() else str(v)
    if isinstance(v, (bytes, bytearray)):
        return {"$bytes": bytes(v).hex()}
    if isinstance(v, (dt.date, dt.datetime, dt.time)):
        return v.isoformat(sep=" ") if isinstance(v, dt.datetime) else v.isoformat()
    return str(v)


def _decode(v):
    if isinstance(v, dict) and "$bytes" in v:
        return bytes.fromhex(v["$bytes"])
    return v


def load_history(path: Path) -> list[tuple[dt.datetime | None, str]]:
    """(when, text) of every run in a prompt_toolkit history file, oldest first: entry N is
    run number N (1 the oldest), as stable as the file, which only grows.

    The file holds `# <timestamp>` and then the query's lines, each `+`-prefixed.
    """
    entries: list[tuple[dt.datetime | None, str]] = []
    when, lines = None, []

    def flush() -> None:
        if lines:
            entries.append((when, "\n".join(lines)))

    try:
        content = path.read_text(errors="replace")
    except OSError:
        return []
    for line in content.splitlines():
        if line.startswith("# "):
            flush()
            lines = []
            try:
                when = dt.datetime.fromisoformat(line[2:].strip())
            except ValueError:
                when = None
        elif line.startswith("+"):
            lines.append(line[1:])
    flush()
    return entries


def history_view(entries: list[tuple[dt.datetime | None, str]], limit: int) -> list[tuple[int, dt.datetime | None, str]]:
    """(number, when, text) for the History tab: newest first, a query run again shown once,
    under its latest number."""
    seen, out = set(), []
    for n in range(len(entries), 0, -1):
        when, text = entries[n - 1]
        if text not in seen:
            seen.add(text)
            out.append((n, when, text))
            if len(out) >= limit:
                break
    return out


def history_stamp(when: dt.datetime | None, now: dt.datetime) -> str:
    """`HH:MM` for today, `MM-DD` before that, blank when unknown (5 cells either way)."""
    if when is None:
        return "     "
    return when.strftime("%H:%M") if when.date() == now.date() else when.strftime("%m-%d")


def save_last(folder: Path, record: dict) -> None:
    """Write the last run atomically, so a killed terminal never leaves half a file."""
    data = dict(record)
    data["rows"] = [[_encode(v) for v in row] for row in record.get("rows") or []]
    data["saved_at"] = dt.datetime.now().isoformat(timespec="seconds")
    tmp = folder / "last.json.tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(data, f, ensure_ascii=False)
    tmp.replace(folder / "last.json")


def load_last(folder: Path) -> dict | None:
    try:
        data = json.loads((folder / "last.json").read_text())
    except (OSError, ValueError):
        return None
    data["rows"] = [tuple(_decode(v) for v in row) for row in data.get("rows") or []]
    return data


def _encode_rows(rows) -> list:
    return [[_encode(v) for v in row] for row in rows or []]


def _decode_rows(rows) -> list[tuple]:
    return [tuple(_decode(v) for v in row) for row in rows or []]


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(data, f, ensure_ascii=False)
    tmp.replace(path)


def save_snapshot(folder: Path, snap: dict) -> None:
    """Write F1's snapshot: `cells` each with its `rows`, encoded like the last run's."""
    data = dict(snap)
    data["cells"] = [dict(c, rows=_encode_rows(c.get("rows"))) if "rows" in c else c
                     for c in snap.get("cells") or []]
    data["saved_at"] = dt.datetime.now().isoformat(timespec="seconds")
    _write_json(folder / "snapshot.json", data)


def take_snapshot(folder: Path) -> dict | None:
    """The snapshot F1 left, once: it is moved aside to snapshot.prev.json as it is read, so a
    later start without F1 goes back to the last run alone."""
    path = folder / "snapshot.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    try:
        path.replace(folder / "snapshot.prev.json")
    except OSError:
        pass
    data["cells"] = [dict(c, rows=_decode_rows(c.get("rows"))) if "rows" in c else c
                     for c in data.get("cells") or []]
    return data


def save_schema(folder: Path, data: dict) -> None:
    """The schema tab's place and the collected catalog (the caller makes it JSON-ready)."""
    _write_json(folder / "schema.json", dict(data, saved_at=dt.datetime.now().isoformat(timespec="seconds")))


def load_schema(folder: Path) -> dict | None:
    try:
        data = json.loads((folder / "schema.json").read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save_chat(folder: Path, turns: list[dict]) -> None:
    """The AI tab's questions and answers (the caller keeps only the latest ones)."""
    _write_json(folder / "ai.json", {"turns": turns, "saved_at": dt.datetime.now().isoformat(timespec="seconds")})


def load_chat(folder: Path) -> list:
    try:
        data = json.loads((folder / "ai.json").read_text())
    except (OSError, ValueError):
        return []
    return data.get("turns") or [] if isinstance(data, dict) else []
