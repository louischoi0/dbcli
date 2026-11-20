# dbcli

A full-screen terminal SQL client for **Oracle**, **PostgreSQL** and **SQLite**, with vim-style
editing, notebook-like cells, Oracle autotrace and an AI assistant panel.

- **Cells**: each statement you run stays on screen as a numbered cell (`#c0 conn0>`, `#c1 conn0>`, …)
  with its result; move back up, edit and re-run any of them.
- **vim keys** in the input area (emacs mode optional), `Ctrl-U` to format SQL, `Tab` completion
  for tables, columns, Oracle hints, `$abbreviations` and `@template` calls.
- **Result grid**: row / cell selection, sort by column, resize columns with the mouse, copy a cell
  or a row to the clipboard (OSC 52 / tmux), and edit a cell in place: `i`, change it, `Enter` runs the
  `UPDATE` in a new cell.
- **Side panel** with tabs: History, Schema browser, Trace (execution plan with E-Rows vs A-Rows,
  statistics), Monitor (sessions), Bookmark (saved queries; one with an `@alias` is a template) and AI.
- **Autotrace** (Oracle, on by default): every run shows `DBMS_XPLAN.DISPLAY_CURSOR` (ALLSTATS LAST)
  and the session statistics.
- **AI tab**: ask Claude about your SQL; `@c0` sends cell #c0's SQL and its last error as context,
  and the suggested SQL goes into the input with one key. Result rows are never sent.
- **Command palette** (`Ctrl-P`): every command with its shortcut, searchable.
- Up to four connections per terminal (`conn0` … `conn3`); history, last results and the AI chat
  are kept per connection across restarts.

## Install

Python 3.11 or newer. Oracle connects in thin mode, so no Instant Client is needed.

```bash
git clone https://github.com/louischoi0/dbcli.git
cd dbcli
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Quick start

```bash
./dbcli connection add          # asks for name, type, host, port, service / database, account, password
./dbcli                         # opens the connection added last
./dbcli prod-ora                # or a named one
./dbcli shop.db                 # or a SQLite file
```

With no connection configured, an in-memory SQLite database opens, so you can try it right away.

Type a query and press **`Ctrl-O`** (or `Alt-Enter` / `F5`) to run it. `Ctrl-P` lists everything
else; `Ctrl-Q` quits.

### Connections

```bash
./dbcli connection list         # * marks the default
./dbcli connection set NAME     # make it the default
./dbcli connection delete NAME
```

Connections live in `.dbclidata/config.toml` (owner-only, `0600`, git-ignored). You can also edit
it by hand; `config.example.toml` shows every option.

### AI tab (optional)

```bash
./dbcli subscription add work   # an Anthropic API key, asked without echo and checked with the API
./dbcli subscription list
./dbcli subscription set NAME
```

Then press `Alt-6` in dbcli and type at the `prompt>` line. `ANTHROPIC_API_KEY` works too.
Without a key, everything except the AI tab works as usual.

## Main keys

| Key | Does |
|---|---|
| `Ctrl-O`, `Alt-Enter`, `F5` | Run the input |
| `F4` / `O` (NORMAL) | Run only the statement / the line under the cursor |
| `Ctrl-N` | New cell |
| `Ctrl-R` | Bookmark the cell's SQL (an optional memo and `@alias`; `U` redoes in NORMAL mode) |
| `$` / `%` (NORMAL) | Top / bottom cell |
| `Ctrl-U` | Format the cell's SQL |
| `Alt-8` / `Alt-9` / `Alt-0` | Focus the prompt / side panel / result pane |
| `Alt-1` … `Alt-6` | Panel tab: History / Schema / Trace / Monitor / Bookmark / AI |
| `Ctrl-W h j k l` | Move between panes |
| `F7` / `F8` | Autocommit / autotrace on or off |
| `Ctrl-P` | Command palette |
| `Ctrl-B` | Hide / show the panel |
| `F1` | Save the session and quit (restored on the next start) |
| `Ctrl-Q` | Quit |

The full reference (every key, completion, templates, autotrace, the schema browser, config
options and stored files) is in **[MANUAL.md](MANUAL.md)**.

## Development

```bash
.venv/bin/python -m unittest    # unit tests
```

The source is in `src/dbcli/`; `./dbcli` runs it with the `.venv` Python. MANUAL.md section 9
describes each module.
