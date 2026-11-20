# dbcli manual

A full-screen terminal SQL client for Oracle, PostgreSQL and SQLite.
One terminal runs one connection: open a terminal per connection you work with.

---

## 1. Install

Python 3.11 or newer. Oracle connects in thin mode, so no Instant Client is needed.

```bash
cd oracle-cli
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 2. Run

```bash
./dbcli                      # the connection added last to .dbclidata/config.toml
./dbcli prod-ora             # that connection
./dbcli shop.db              # a SQLite file (a path, or a .db / .sqlite / .sqlite3 name)
./dbcli --list               # the connections, the default one marked
./dbcli --config my.toml     # another config file
```

### Managing connections

```bash
./dbcli connection list            # the connections; * marks the default
./dbcli connection add             # asks for name, type, host, port, service / database, account, password
./dbcli connection add ora2 --type oracle --host db1 --port 1521 --service ORCL --user scott
./dbcli connection set prod-ora    # make it the default (./dbcli opens it)
./dbcli connection delete ora2     # asks first; -y does not
```

- **`add`** asks for whatever the flags do not give: the connection name, the type (`oracle` by default), then for Oracle the host (`localhost`), port (`1521`), service name, account and password; for PostgreSQL the host, port (`5432`), database, account and password; for SQLite the file. The password is typed without showing and stored in `config.toml` (empty: none). `--password-env VAR` stores `password_env = "VAR"` instead, so the password is read from `$VAR` at connect time. At the end it offers to connect once to check it (`--test` does it, `--no-test` skips the question); a failed test leaves the connection saved.
- A new connection **becomes the default**, as the last one in the file is; `--keep-default` adds it just above the current default instead.
- **`set NAME`** moves the connection's block to the end of the file, which makes it the default. **`delete NAME`** removes the block, password included.
- `config.toml` is edited as text: comments and the other sections stay as written (a comment just above a connection, set off by a blank line, moves and goes with it). Each change is checked to parse before it is saved; the file stays owner-only (`0600`). `--config FILE` (after `connection`) works on another file. `connections` and `conn` work as `connection`.

### The AI tab's keys (subscriptions)

```bash
./dbcli subscription add work                 # a name and its Anthropic API key (asked, not shown)
./dbcli subscription add home --model claude-sonnet-5-5 --effort low
./dbcli subscription list                     # * marks the active one
./dbcli subscription set home                 # make it the active one (no name: asks which)
./dbcli subscription show [--check]           # the active key masked (sk-ant-…abcd), model, effort; --check asks the API
./dbcli subscription delete home              # asks first; -y does not
./dbcli subscription unset                    # none active (the subscriptions are kept)
```

- Each subscription is a `[subscriptions.NAME]` block in `config.toml` with its `api_key` and, if given, its own `model` and `effort` (else `[ai]`'s); `[ai] subscription = "NAME"` says which one the AI tab uses. The first one added becomes the active one; `add --use` makes a later one active.
- `set` with no name lists them and asks which (with none yet it adds the first). After `add` it asks the API about the model with the key (`models.retrieve`, nothing billed); `--no-check` skips that, and it needs the Anthropic SDK (6.4).
- Like `connection`, the file is edited as text, checked to parse, and stays `0600`. A key is never printed in full. `sub` works as `subscription`.
- Without an active subscription, `[ai] api_key` (older configs) or `ANTHROPIC_API_KEY` is used; failing those, the AI tab shows the subscriptions to pick from (6.4).

- With no argument the **connection added last** (lowest in the config file) opens. With no connections at all, an in-memory SQLite database (`memory`) opens.
- Connection details and passwords are all in `.dbclidata/config.toml`, so `./dbcli` needs no environment variables or other setup (section 7).
- A name that is neither a connection nor looks like a file is refused, with the list of connections.
- Each connection keeps its own query history, last run, result and trace (section 8). Opening the same connection again brings them back.

## 3. Screen

The top bar, the shell on the left, the panel on the right, and the result pane across the bottom (about 40% of the height).

```
 INSERT ● prod-ora online │ prod-ora: done │ autocommit off F7 │ autotrace on F8 │ oracle · scott@db:1521/ORCL
──────────────────────────────────────────────────────────────────────────
 > select count(*) from orders               │ History  Schema  Trace  Monitor ─
 (1 row)                                     │ select count(*) from orders
                                             │ select id, name from customers…
 > select id, name                           │
   from customers where id < 3               │
 (2 rows)                                    │
                                             │
 > ▏                                         │
── Result · prod-ora · select id, name from customers where id < 3 (2 rows) ──
 ┌────┬──────┐
 │ ID │ NAME │
 ├────┼──────┤
 │  1 │ kim  │
 │  2 │ lee  │
 └────┴──────┘
```

| Area | What it is |
|---|---|
| Top bar | From the left: vim mode, connection state (●), connection name, online state, the latest message, the **autocommit** and **autotrace** switches with their keys, and the target. A narrow terminal cuts from the right, so the important parts come first. The right end lists the main keys. |
| Shell | The `> ` input area works **like a text editor**: Enter is a new line and **Ctrl-O** runs. A cell of several lines ends with `_ ` on its last line, so `>` … `_` mark where each block starts and ends. The shell keeps each statement run and a short outcome (`(2 rows)`, `OK, 3 row(s) affected`, errors). |
| Panel | One panel with the **History / Schema / Trace / Monitor / Bookmark / AI** tabs (`Alt-1` to `Alt-6`). The panel is as wide as a plan needs on every tab (at most 96 columns, half the screen), so switching tabs never moves the shell. The first tab shown is `[ui] panel` (section 7). |
| Result | The latest result as a **boxed table**. Each header cell has the column name on the left and its type on the right (`N` number, `VCH` varchar2, `CH` char, `CL` clob, `BB` blob, `R` raw, `D` date, `T` timestamp, `TZ` timestamp with time zone, `RID` rowid …; Oracle only), the type in a toned-down shade of its values' color. Values are colored by the column's type (numbers green, text yellow, dates lavender, binary teal, booleans orange), numbers right-aligned, NULL dimmed, every other row shaded. Cells are cut at 40 columns with `…`, line breaks show as `↵`. |

Connection state in the top bar:

| Shows | Means |
|---|---|
| `● online` | Connected. dbcli does not ping on its own: a lost connection shows (`offline`) when a run of yours meets it, and the next run reconnects. |
| `● offline` | The check was not answered. The next query reconnects first. |
| `● connecting…` / `· running…` | Connecting, or running a statement. |
| `● connection failed` / `● not connected` | The connection failed, or has not been made yet. |

Panel tabs:

| Tab | Shows |
|---|---|
| History | This connection's queries, newest first, up to 500. Each row starts with its run number and when it ran (`HH:MM` today, `MM-DD` before). Numbers count up like an auto-increment: the first query ever run on the connection is 1, each run takes the next one, and they never change. A query run again moves to the top under its new number. Hints are colored as in the input area. |
| Schema | A schema browser (section 6.2): the kinds of objects with their counts, a kind's objects, then one object's details (columns, indexes, constraints, triggers, arguments, compile errors) and its DDL. |
| Monitor | Sessions, blocking, locked objects, top SQL and wait events, read when you press Ctrl-O in the tab (section 6.3). |
| Bookmark | Saved queries (section 5.4), one to a line: id, `@alias` (a template), query, and the memo at the right end. Enter puts it in the prompt, `e` edits it right in the panel (vi keys, `:wq`); `a` adds, `A` (or `Ctrl-R` in a cell) bookmarks the current cell, `t` sets the alias, `d` deletes. |
| AI | Questions about SQL to Claude, answered in the tab (section 6.4). |
| Trace | With autotrace on (Oracle): the run's **estimated plan** (EXPLAIN PLAN + DBMS_XPLAN), **actual plan** (DISPLAY_CURSOR `ALLSTATS LAST`) and **session statistics**, drawn as a tree (section 6.1). It shows the trace of the cell focused in the shell. |

Colors are soft grays with sky blue: the focused pane's title (the selected tab in the panel), the prompt, table headers and `online` are sky blue; `connected` in the top bar's message is green; only offline and errors are red. Orange marks `conn0` in a cell's prompt, an `off` switch in the top bar (`autocommit off`, `autotrace off`) and the numbers in the panel (History's run numbers, Bookmark's ids). Ctrl-B hides the panel so the shell takes the full width.
The layout follows the terminal's size. Panes, tabs, list items and the two switches in the top bar can be clicked.

## 4. Keys

### Running

| Key | Does |
|---|---|
| **`Ctrl-O`** | Runs the whole input area (no `;` needed), in INSERT and NORMAL mode. |
| **`$`** (NORMAL) | Jumps to the top cell. `d$`, `c$` and `$` in VISUAL keep their usual meaning. |
| **`%`** (NORMAL) | Jumps to the bottom cell (the input cell), at the end of its text, staying in NORMAL mode. `d%`, `c%` and `%` in VISUAL keep their usual meaning (vim's "matching bracket" is replaced). |
| **`Ctrl-N`** | A new empty cell below the cell under the cursor, focused in INSERT mode (with the completion menu open, Ctrl-N still moves in it). |
| `Alt+Enter`, `F5` | The same. |
| `Ctrl+Enter` | The same, where the terminal sends Ctrl+Enter distinctly from Enter (below). |

> vim's INSERT-mode `Ctrl-O` (one NORMAL-mode command) becomes the run key.

> **For Ctrl+Enter:** if it makes a new line or does nothing, the terminal sends it as plain Enter. dbcli asks the terminal to tell it apart at start (xterm `modifyOtherKeys`); xterm does, others vary. **Inside tmux** this is needed (in `~/.tmux.conf`, then `tmux source ~/.tmux.conf`):
>
> ```
> set -s extended-keys on
> set -as terminal-features 'xterm*:extkeys'
> ```
>
> Reattach to tmux afterwards (detach, then `tmux attach`) so the outer terminal picks it up.
>
> `.venv/bin/python scripts/keytest.py` shows what the terminal sends: Ctrl+Enter should show `'\x1b[27;5;13~'` or `'\x1b[13;5u'`; `'\r'` means the difference was lost on the way (q quits).
>
> Otherwise use Alt+Enter or F5.

### Input area (vim mode, the default)

It starts in INSERT mode; Esc goes to NORMAL. The mode shows at the start of the top bar (the cursor shape changes too).
After a run it returns to INSERT mode, ready for the next query.

| Mode | Main keys |
|---|---|
| INSERT | typing, `Enter` new line, `Tab` input helpers (section 5), `Esc` → NORMAL |
| NORMAL | `h j k l` `w b e` `0 $` `gg G` move · `i a I A o O` insert · `x dd dw D` delete · `cw cc C` change · `yy p P` yank and put · `u` undo · `U` redo (Ctrl-R bookmarks the cell, below) · `v V` select · counts (`3dd`) |
| Both | `↑` / `↓` on the first / last line: the previous / next cell (section 6, "Cells"; `k` / `j` in NORMAL) · `Ctrl-C` cancels a running statement (on its cell; on an empty input, or in the panel or the result pane, everything running: Oracle answers ORA-01013 and the connection stays usable), else clears the input cell, or drops the edits of an earlier cell · `Ctrl-L` clears the shell's cells · `Ctrl-D` on an empty input quits |

- **`Ctrl-U` formats the cell under the cursor** (sqlparse): one select item to a line, `FROM` / `JOIN` / `WHERE` / `AND` / `GROUP BY` on lines of their own, subqueries indented, spaces around operators. Keywords and names keep the case you typed; comments and hints stay. `u` (NORMAL mode) undoes it. PL/SQL blocks and `@template` calls are left alone. (It replaces vi's "delete to the line's start"; in the panel and the result pane Ctrl-U still scrolls.)
- `L` (Shift+L) in NORMAL mode clears the focused cell; `u` brings the text back. (vim's "cursor to the bottom of the screen" is replaced. In INSERT mode Shift+L types a capital L.)
- `u` undoes a whole insert (`i` … `Esc`) at once, as in vim.
- `cw` changes to the end of the word and keeps the following space, as in vim.
- For emacs keys set `[ui] editing_mode = "emacs"` (Ctrl-A/E line start and end, Ctrl-K kill to end of line, and so on).

### Moving between panes (vim style)

| Key | Does |
|---|---|
| `Alt-8` | The prompt (the input cell), from any pane, in INSERT mode |
| `Alt-9` | The side panel, on the tab it shows (shown again if Ctrl-B hid it) |
| `Alt-0` | The result pane |
| `Alt-1` … `Alt-6` | Opens the panel on History / Schema / Trace / Monitor / Bookmark / AI |
| `Ctrl-W h` / `j` / `k` / `l` | The pane to the left / below / above / right |
| `Ctrl-W` + arrow, `Ctrl-W Ctrl-H/J/K/L` | The same |
| `Ctrl-W w`, `Ctrl-W Ctrl-W` | The next pane |
| `Ctrl-W W` | The previous pane |
| `Ctrl-W` alone | After a second, deletes the word before the cursor (INSERT mode's own) |

Directions follow where the panes are drawn: from the prompt `Ctrl-W l` is the panel, `Ctrl-W j` the result pane, `Ctrl-W k` the shell above (to scroll back).

### Result pane

| Key | Does |
|---|---|
| `j` / `k`, `↓` / `↑` | Next / previous row (the row gets a blue-gray background, the selected cell white bold on blue; the title shows `row 3/20`) |
| `h` / `l`, `←` / `→` | Previous / next column (a wide table scrolls sideways) |
| `Ctrl+PageDown` / `Ctrl+PageUp` | Scroll the result pane a screen at a time, **wherever the focus is** (plain `PageUp` / `PageDown` scroll the panel) |
| `Ctrl-D` / `Ctrl-U`, mouse wheel | Scrolls half a screen / a line while the result pane has focus; the view and the selected row move together |
| `g` `Home` / `G` `End` | First / last row |
| `0` / `$` | First / last column |
| `y` / `Y` | Copies the selected cell's value / the selected row (tab-separated) to the clipboard: in full, NULL as nothing, binary as hex. Inside tmux it goes through `tmux load-buffer -w`, otherwise an OSC 52 escape, so the terminal's clipboard gets it (with tmux `set-clipboard` on or external, and a terminal that takes OSC 52). vi's `p` in the input area pastes it too |
| Click a column's header, or `s` on the selected column | Sorts the rows by it: ascending (`▲` after the name), descending (`▼`), then back to the order fetched. NULLs go last. It sorts the rows on screen (up to 1000), it does not query again |
| Drag a `│` border with the mouse | Makes the column left of it wider or narrower (1 to 400 cells; text that no longer fits ends in `…`). The width stays with that result, also when you come back to its cell |
| `i` / `a` / `Enter`, `I`, `c` | Edits the selected cell's value on a line under the table (cursor at the end, at the start, or from empty), in INSERT mode with the usual vim keys. `Enter` there runs an `UPDATE` for it: the statement goes into a new cell at the bottom of the shell, with its outcome (`OK, 1 row(s) affected`) under it, and the grid shows the new value. `Esc` in NORMAL mode or `Ctrl-C` drops the edit. Type `NULL` for NULL; numbers are checked; Oracle dates take `YYYY-MM-DD[ HH24:MI[:SS[.FF]]]` |

Editing works on the result of a query on one plain table (no join, subquery, union, `GROUP BY`, `DISTINCT` or `WITH`). The row is found by its `ROWID` when the result shows one (`select rowid, e.* from emp e`), otherwise by every column it shows that is a real column of the table (aliases and expressions are left out, as are LOBs, binary values, and floats when other columns exist); on Oracle `and rownum = 1` keeps a duplicate row from changing twice. With autocommit off the change waits for your `commit;`. If the UPDATE matches no row (someone changed it since), the status line says so and the grid stays as it was.

### Panel

| Key | Does |
|---|---|
| `→` / `←`, `l` / `h`, `Tab` / `Shift-Tab` | Next / previous tab (wrapping around) |
| `1` … `6` | History / Schema / Trace / Monitor / Bookmark / AI |
| `j` / `k`, `↓` / `↑` | Moves the selection |
| `g` / `G`, `Home` / `End` | Top / bottom |
| `PageUp` / `PageDown` | Scroll the panel a screen at a time, **wherever the focus is** (the prompt, the result pane, the panel) |
| `Ctrl-U` / `Ctrl-D`, mouse wheel | Scroll half a screen / a line while the panel has focus; a scrollbar shows on the right |
| `Enter`, click | Uses the selected item (below) |

| Tab | Enter |
|---|---|
| History | Puts the query in the input area, without running it: edit, then Ctrl-O. |
| Schema | Opens the kind or the object under the selection (section 6.2). |

### Command palette (`Ctrl-P`)

`Ctrl-P` (anywhere, any mode) opens a list of every command with its shortcut, like VS Code's. Type to search: each word must appear in the command's name, its group or its key (`run`, `format`, `ctrl-u`, `templ`), and the words show in bold blue. `↑` / `↓` (or `Ctrl-P` / `Ctrl-N`, `Ctrl-K` / `Ctrl-J`, `PageUp` / `PageDown`) choose, `Enter` or a click runs it, `Esc` closes the list.

A command does exactly what its key does, where that key works: a cell command (run, format, new cell, conn0…3) acts on the cell you were in (or the cell edited last), a tab command opens its tab first (Schema's Ctrl-O, Bookmark's `a`), a result command works on the result pane. With the completion menu open, `Ctrl-P` still moves up in it. (vim's NORMAL-mode Ctrl-P, "line up", and emacs mode's "previous line" are replaced; use `k` / `↑`.)

### Switches and the rest

| Key | Does |
|---|---|
| Time zone | Every time dbcli shows or records (history, run times) is Korea time (KST). Another zone: `DBCLI_TZ=Europe/Berlin ./dbcli`. Times inside query results are the database's own |
| `F7`, or click `autocommit` in the top bar | Autocommit on / off. Turning it on while changes are pending: they commit with the next statement. PostgreSQL refuses while a transaction is open; SQLite always commits. |
| `F8`, or click `autotrace` in the top bar | Autotrace on / off (Oracle). While on, every run opens the Trace tab. |
| `Ctrl-B` | Hides / shows the panel |
| `Ctrl-Q` | Quits |
| `O` (Shift+O, NORMAL) | Runs only **the line the cursor is on**, in place, like `F4` (the cell's result becomes that line's, no cell is added). With `select 1 from dual;` / `union all` / `select 2 from dual;` on three lines and the cursor on the first, only the first `select` runs. An empty line says so. vim's "open a line above" is replaced (`k` then `o` does the same) |
| `F4` | Runs only the complete statement under the cursor, in place: the cell's result becomes that statement's, and no cell is added. In a cell of several statements it takes the one the cursor is in, ended by `;` (a PL/SQL block counts whole); a cell of one statement needs no `;`. It runs on the cell's connection: cells on different connections run at the same time, cells on one connection one after another. Any statement: queries, DML, DDL, PL/SQL |
| `Ctrl-0` `Ctrl-1` `Ctrl-2`, `Ctrl-W` `0`…`3` | Puts the cell on connection conn0 … conn3 (section 6, "Connections"). Terminals send Ctrl-3 as Esc, so conn3 is `Ctrl-W 3` (works for all four) |
| `F1` | Saves a snapshot of the session and quits: every shell cell with its result, the prompt's text and cursor, each result's fetched rows (all pages so far, column widths and sort), the focus and the result pane's place. The next `./dbcli` brings it back once; a large result's unfetched rest needs a re-run, as its cursor does not survive |

> `Ctrl-B` replaces emacs mode's "cursor left"; use the arrow keys to move.

## 5. Input helpers

### 5.1 What Tab does, in order

- A `/` right after `SELECT`, `INSERT`, `UPDATE`, `DELETE` or `MERGE` shows `*+ ` in gray; Tab completes it to `/*+ `, ready for a hint.
- Inside `/*+ ... */`, where a hint name goes (right after `/*+`, after a space or after a hint's `)`): **Tab puts in a hint, and each further Tab the next one, through all the Oracle hints** (132), the ones you have run most first, then the common ones (`gather_plan_statistics`, `monitor`, `full`, `index` …). With letters typed (`no_u` + Tab) only the hints that start with them. A hint is lowercase, or in capitals when the statement is typed in capitals. Hint use is counted apart from the same words in SQL (the hint `full` is not FULL OUTER).
0. A template call (input starting with `@`, or an `@alias` inside a line after a space, `(`, `,` or `=`): completes the template's name, or with arguments, expands it into the full query at the cursor (5.4).
1. `!N` before the cursor (`!3`): replaced by run number N, as the History tab shows it (1 is the oldest; numbers a repeat left behind still work). A number with no entry shows an error in the top bar.
2. `$name` before the cursor and a known abbreviation: replaced by its text.
3. Nothing typed yet where a table goes (right after `FROM`, `JOIN`, `INTO`, `UPDATE`, or after a comma in a FROM list): **Tab puts a table name in, and each further Tab the next one** (your schema's own tables and views from the catalog `Ctrl-O` collected in the schema tab, and other tables you have used, best first: how often and how lately you used them, and how often they followed that word); after the last one the slot is empty again. This also follows a completed keyword: `select * fr` + Tab gives `from `, and the next Tab goes on to the tables (not to other words starting with `fr`). Likewise **after `ON`** (and after `=` or `AND` in a join condition) Tab puts in `alias.column` of the joined tables: after `ON` the columns the joined table shares with the earlier ones first (the likely join keys), after `a.x = ` the other tables' `x` first (from the collected catalog). And **after `WHERE` or `HAVING`** (and after `AND`, `OR`, `NOT` or `(` in it) Tab puts in the columns of the statement's tables, the ones you use most first: bare `column` for one table without an alias, else `alias.column`. Also **in a SELECT list** (right after `SELECT`, `DISTINCT` or a hint, or after a comma in the list): the columns of the tables its FROM names, even when the FROM comes after the cursor (`select ▏ from t a join t0 b …` + Tab → `a.v`, then `b.v`); with no FROM written yet there is none, and Tab opens the menu. And **after `ORDER BY` or `GROUP BY`** (and after a comma in its list) the same: the statement's tables' columns. And **after an UPDATE's `SET`** (and after a comma in its list) the updated table's columns, bare (in a MERGE's `update set`, the INTO table's). And **in an INSERT's column list** (right after `insert into t (`, or after a comma in it) the table's columns not listed yet, bare (in a MERGE's `insert (`, the INTO table's).
4. A word being typed: replaced by the best candidate, with one space after (none if a space already follows). **Each further Tab puts the next candidate in its place** (up to 15, in ranking order; the top bar shows `Tab 2/6: to_date`), and after the last one the word as you typed it comes back. Typing `(` `,` `)` `.` or `;` right after takes that space back: `to_char(`.
5. Nothing typed yet (anywhere else): opens the completion menu. In the menu Tab goes to the next item and Enter picks.

### 5.2 Abbreviations

Type `$a`, then Tab → `select * from `

Built in:

| Abbreviation | Becomes |
|---|---|
| `$a` | `select * from ` |
| `$c` | `select count(*) from ` |
| `$w` | `where ` |
| `$o` | `order by ` |
| `$g` | `group by ` |

`[abbreviations]` in the config adds or overrides them (section 7).
A `$` inside a word, as in `v$session`, is not an abbreviation.

### 5.3 Gray suggestions (inline completion)

After two letters the rest of a word shows in gray after the cursor.

```
se▏lect        ← "lect" in gray. Tab → "select "
SEL▏ECT        ← typed in capitals, suggested in capitals
from my▏Table  ← Tab → "from MyTable " (the stored spelling, the typed part included)
```

- **Words:** SQL keywords, the connected database's table and column names, the columns of every table a statement has used (synonyms and `V$` views included), and the words of queries you ran.
- **Score:** a base weight plus the number of uses. Keywords weigh 2 to 50 (`select` and `from` the most), schema names 5. Every run adds 1 to each of its words.
- **Table names where a table goes:** right after `FROM` (and after a comma in its list), `JOIN`, `INTO` or `UPDATE`, this schema's own tables and views come first, then every other table, view and synonym the user can see (`dual`, `user_tables`, `v$sesstat` …), which are offered only there.
- **Columns of the statement:** elsewhere, the columns of the tables in the statement you are editing come first. Right after `alias.` (or `table.`) only that table's columns are offered: `from v$session s` … `s.us` → `s.user#`, `s.username`.
- **Statistics:** every statement you run is counted: how often each word is used, when it was last used (a word used lately ranks a little higher, fading over a few days), and **which word it followed** (`order` → `by`, `from` → the tables you query, `nvl` → …), so the word before the cursor steers the ranking. They are kept in `words.db`, which every dbcli terminal shares and adds to. In the background, every minute dbcli recounts them (in words.db only), taking in what other terminals learned. **Tables, views and columns** for completion and the red underlines come from the catalog that `Ctrl-O` in the schema tab collects; before that, names are neither offered from the database nor checked.
- **Words offered** besides your own: SQL keywords and the common Oracle functions (`nvl`, `decode`, `to_char`, `to_date`, `trunc`, `substr`, `listagg`, `row_number`, `regexp_like` …).
- **Ties:** shorter words first, then alphabetical.
- **Finished words:** a word already typed in full that scores higher gets no suggestion: `select` does not offer `selectivity`.
- **Case:** names in all capitals or all lower case follow your typing; mixed case (`MyTable`) keeps its spelling.
- **Not learned:** string literals (`'...'`), quoted identifiers, comments.
- **No suggestion** with the cursor inside a word.
- **No context yet:** after `from`, a keyword that scores higher can still come before a table name.

#### Oracle hints

On an Oracle connection the optimizer hints (`gather_plan_statistics`, `use_hash`, `leading`, `index_ffs`, `parallel`, `no_merge` and more) join the suggestions, following your case like keywords: `/*+ gath` → `gather_plan_statistics`, `/*+ GATH` → `GATHER_PLAN_STATISTICS`. In the input area, the History tab and the result title, SQL is drawn in a soft white, keywords (`select`, `from`, `where` …) in bold sky blue, table and view names in bold peach, operators and punctuation (`,` `(` `)` `+` `-` `*` `/` `|` `=` `<>` … and the `/*` `*/` around comments) in a pale pink bold, and hints inside `/*+ ... */` in bold pure white, so they stand out, and what their parentheses name (table aliases, index names, `@query_block`s) in a warm sand color: `/*+ index(e emp_ix) */`. The list is `src/dbcli/oracle_hints.py`.

### 5.3.2 Missing tables, views and columns

A table or view name after `FROM` (and its comma list), `JOIN`, `INTO` or `UPDATE` that the database does not have is drawn **red and underlined** while you type, and so is a **column** none of the statement's tables has. The names are read when the connection opens (Oracle: every table, view and synonym in `ALL_OBJECTS`, so `DUAL` and `V$` views count; PostgreSQL: `pg_class`; SQLite: `sqlite_master`) and again after a `create` / `drop` / `alter` / `rename`.

- Columns: `alias.col` is checked against that alias's table; a bare `col` against all the statement's tables together. A table's columns are read the first time a statement uses it (Oracle: the user's own table or view, or what a synonym of that name points to; PostgreSQL: `information_schema.columns`; SQLite: `pragma table_info`), so the underline can follow a moment later.
- Bare column names are only judged when every table of the statement could be: a subquery, table function, `WITH` name, qualified name or link in a `FROM` leaves them alone. Keywords, functions, select-list aliases (`as total`, `nvl(v, 0) total`), pseudo-columns (`rownum`, `level`, `sysdate` …) and bind variables (`:id`) are never columns. DDL, PL/SQL and `PIVOT` / `MODEL` / `JSON_TABLE` statements are not checked.
- The word still being typed (the cursor at its end) is left alone until you move on.
- Not judged, so never underlined: schema-qualified names (`hr.emp`), database links (`emp@remote`), quoted names, table functions (`table(...)`), names a `WITH` clause defines, and `FROM` inside a function call (`extract(year from d)`).
- Before the connection is up nothing is underlined.

### 5.4 Bookmarks and SQL templates

The **Bookmark** tab (`Alt-5`, below) keeps the queries you use often, each with an optional memo. Giving a bookmark an `@alias` is optional too: one that has an alias is also a **template**, called as `@alias args…`; one without is only a saved query to put in the prompt. Bookmarks are kept in `.dbclidata/bookmarks.json`. On the first start a `templates.json` from before fills it (each template a bookmark with its alias), or else the config's `[templates]` (section 7); after that the file is what counts.

```toml
[templates]
a = "select * from t where num <= $1"
range = """
select *
from t
where num between $1 and $2"""
who = "select * from emp where name = $1"
```

```
> @a 4
-- select * from t where num <= 4        ← the expanded query stays in the shell
(4 rows)                                 ← the table is in the result pane
```

| Input | Does |
|---|---|
| `@a 4` + Ctrl-O | Puts `4` in `$1` and runs. No `;` needed (one is fine). |
| `@range 1 10` | `$1` → `1`, `$2` → `10`. Arguments are split on spaces. |
| `@who 'kim lee'` | A quoted argument is one argument, quotes included → `name = 'kim lee'` |
| `@a 4` + Tab | Expands the query into the input area without running it. |
| `@ra` + Tab | Completes the name → `@range `. With several matches a menu shows names and bodies. |
| `select * from (@a 4` + Tab | Inside a line too: the call from its `@` to the cursor becomes the query, in place → `select * from (select * from t where num <= 4`. The `@` must follow a space, `(`, `,` or `=` (so `emp@dblink` is left alone); a call with the wrong number of arguments inside a line gets the usual Tab. |
| `@` + Ctrl-O | Lists the templates (the bookmarks with an alias) in the shell. |

- Arguments are **substituted as text** (not driver bind variables). For strings write `'$1'` in the template, or pass `'value'`.
- The number of arguments must equal the highest `$n` in the template; otherwise nothing runs and the error shows the template.
- Only `$` followed by digits is an argument, so `v$session` and `$a` abbreviations do not clash.
- While typing, the top bar shows `@a (args 1/1): select …`.
- History keeps the call as typed (`@a 4`).

**Bookmark tab** (`Alt-5`): one line per bookmark: `id @alias query` from the left (the alias column only when some bookmark has one, the query on one line, cut to fit) and the memo at the right end. The id is given when a bookmark is added and never reused.

| Key | Does |
|---|---|
| `Enter` | Puts the query in the prompt (the cell edited last): the whole text of an empty cell, else on a line of its own at the cursor; focus moves there in INSERT mode |
| `i` | For a template, puts `@alias ` at the cursor instead: type its arguments, then Tab. For a plain bookmark, as Enter |
| `e` | Edits the query in the panel, in place of the list, with the input area's vi keys and SQL colors (NORMAL mode to start; `i`, `A` … to type). The header shows `memo @alias #id`, and `[+]` while there are unsaved changes |
| `a` | Adds one: asks for the memo, then the `@alias` (both optional: Enter on an empty answer leaves it out; no alias, no template), under the panel (Enter answers, Esc drops it, Ctrl-U clears), then opens the editor on an empty query in INSERT mode. It is added on the first `:w`; an empty query adds nothing |
| `A`, or `Ctrl-R` in a cell (any mode) | Bookmarks the cell's SQL (for `A`, the cell edited last): asks for the memo and the `@alias`, both optional, and saves it. From a cell, the focus goes back to it |
| `t` | Sets the `@alias`, making the bookmark a template; an empty answer clears it (a plain bookmark again) |
| `r` | Changes the memo (an empty answer clears it) |
| `d` `d` | Deletes the bookmark (the first `d` asks, the second deletes) |
| `y` | Copies the query |

In the editor (NORMAL mode):

| Key | Does |
|---|---|
| `:w` (or `Ctrl-S` in any mode) | Saves |
| `:wq`, `:x`, `ZZ` | Saves and goes back to the list |
| `:q` | Back to the list; refused while there are unsaved changes |
| `:q!`, `ZQ` | Back to the list, dropping the changes (a new bookmark is not added) |

The `:` line opens under the panel: Enter runs it, Esc drops it. Other tabs can be opened meanwhile; `Alt-5` comes back to the editor as it was.

The file is shared by every dbcli terminal: a change made in one shows in the others.

## 6. Running queries

- **Ctrl-O** (or Alt+Enter, F5, Ctrl+Enter) sends the **whole** input area as one statement. No `;` needed at the end.
- A cell runs in place: its result shows under it and the cursor stays. **Ctrl-N** adds an empty cell right below the one under the cursor (at the bottom it becomes the new input cell), for the next statement. The statement also goes to the History tab, unless the database rejected it as a syntax or parse error (Oracle ORA-009xx such as ORA-00900, 00904, 00933, 00942, and PL/SQL compile errors; PostgreSQL SQLSTATE class 42; SQLite syntax and "no such" errors). Such a statement is not learned by the suggestions either. Other errors (a constraint, a lost connection) are kept.
- Send one statement at a time: several joined with `;` make the database report an error.
- **Oracle PL/SQL blocks** run as they are. A last line of `/`, as in SQL*Plus scripts, is dropped.

  ```sql
  begin
    dbms_output.put_line('hi');
  end;
  /
  ```
- Query results go to the result pane, the row count to the shell, with when the run finished and how long it took: `(6 rows)  ✓ 09:29:46 · 15ms` (the date too when it was not today). `OK, N row(s) affected` from `insert` / `update` and errors show in the shell; the result pane keeps the last query result.
- **Large results come in pages:** the first 500 rows show, with `(500 rows, more ↓)`. Scrolling down near the end in the result pane (`j`, `↓`, `Ctrl-D`, `Ctrl+PageDown`, `G`, the wheel) fetches the next 500 from the still open cursor, until `(1234 rows)` says it is all there. Columns widen for longer values that arrive (not one whose width you dragged), and a sorted result keeps its sort. A session keeps at most 5 such cursors open (the oldest results stop fetching more); a re-run closes its cell's old one. An autotraced run still fetches up to 1000 rows at once.
- Queries and connecting run in the background, so the screen never freezes. Statements on one connection run one at a time, in order.
- **Nothing goes to the database unless you ask**: connecting opens the connection only; the catalog is read by `Ctrl-O` in the schema tab, the monitor by `Ctrl-O` in its tab, an object's details by Enter / `d` in the schema tab. No ping, no periodic refresh.
- The shell keeps the latest 200 cells; older ones are dropped.
- An error shows under its cell and in the result pane as its code and message (Oracle's `Help: https://…` link is left out), and the connection is checked at once so online/offline stays right.
- A successful `create` / `drop` / `alter` / `rename` reloads the Schema tab's counts and list, the completion's names and the red-underline check.
- After each run the run itself (text, outcome, result rows, trace) is saved for this connection, and comes back as the first cell the next time it opens (`-- last run, saved …`).

### 6.1 Autotrace (Oracle)

It is **on from the start**; **F8** (or a click on `autotrace` in the top bar) turns it off and on again. Run a statement with it on and the Trace tab opens with:

```
▌ Execution Plan · estimated
  Plan hash value: 2344062168
  0  SELECT STATEMENT                                      rows 1 · cost 5
  1  └─ SORT GROUP BY                                      rows 1 · cost 5
  2     └─ NESTED LOOPS                                    rows 1 · cost 4
  3        ├─ INLIST ITERATOR
  4        │  └─ TABLE ACCESS BY INDEX ROWID  DEPT_TV      rows 2 · cost 2
  5*       │     └─ INDEX UNIQUE SCAN         SYS_C008718  rows 2 · cost 1
  9*                └─ INDEX RANGE SCAN       EMP_TV_DEPT  rows 200 · cost 1

  Predicates
  5  access("D"."ID"=2 OR "D"."ID"=3)

▌ Actual Plan · DISPLAY_CURSOR
  …                                       estimated 200 · actual 400 · buffers 5 · 0.01s

▌ Statistics
          7  consistent gets
          3  rows processed
```

- **Estimated plan:** EXPLAIN PLAN + DBMS_XPLAN.DISPLAY, with `rows · cost`.
- **Actual plan:** DBMS_XPLAN.DISPLAY_CURSOR `ALLSTATS LAST` with `estimated` (estimated rows) and `actual` (actual rows, its label in bold orange), then buffers and time. The traced statement alone runs with `statistics_level = all`, so no `gather_plan_statistics` hint is needed. An actual row count 10 times or more off the estimate is highlighted.
- **Statistics:** the session's V$MYSTAT counters over the statement, as SQL*Plus autotrace lists them; zeros are dimmed; **recursive calls, consistent gets and rows processed** stand out, the name in bold ivory and the value in bold orange (zeros too), physical reads, db block gets and redo size stand out in blue.
- Operations are drawn as a tree (`├─ └─ │`), `*` marks a step with a predicate, and each kind of operation has its own color:

| Operation | Color |
|---|---|
| `TABLE ACCESS FULL` (full scan) | red |
| `INDEX FULL / FAST FULL / SKIP SCAN` | orange |
| `INDEX RANGE / UNIQUE SCAN` (index lookup) | green |
| `TABLE ACCESS BY INDEX ROWID` (key lookup) | teal |
| `NESTED LOOPS` | blue |
| `HASH JOIN` | yellow |
| `MERGE JOIN` / `SORT JOIN` | purple |
| `SORT`, `GROUP BY`, `WINDOW` | pink |

- Predicates show `access` in green and `filter` in yellow.
- The user needs to read a few views; without them the trace says which grant is missing:

  ```sql
  -- as SYS, in the PDB
  grant select on v_$mystat to <user>;
  grant select on v_$statname to <user>;
  grant select on v_$session to <user>;
  grant select on v_$sql to <user>;
  grant select on v_$sql_plan to <user>;
  grant select on v_$sql_plan_statistics_all to <user>;
  ```

### 6.2 Schema browser

`Alt-2` opens it. What it last showed, and the catalog Ctrl-O collected, are kept per connection (`schema.json`): after a restart the tab is where you left it, even before the connection is back, and completion has the catalog at once. Ctrl-O reads it all again. Three levels:

1. **Kinds** with their counts: tables, views, materialized views, indexes, sequences, synonyms, triggers, procedures, functions, packages, types (Oracle: the user's own objects; PostgreSQL: the current schema; SQLite: all).
2. **Objects** of a kind, with when each last changed; an invalid one shows red with `INVALID`.
3. **Details** of an object, as small tables:
   - table / view: its properties (row count from statistics, tablespace, comment), **columns** (type, `not null`, comment), **indexes** (unique or not, columns), **constraints** (primary / unique / foreign key with the table it refers to, check with its condition), **triggers**;
   - index: its table and columns; sequence: its settings; synonym: what it points to; trigger: its table, timing and event; procedure / function: its arguments; package: its subprograms;
   - and the **compile errors** of a view, trigger or PL/SQL unit that has them.

| Key | Does |
|---|---|
| `Enter` | Opens the kind / object under the selection (on the `◂` line: back) |
| `Backspace` | Back one level |
| `d` | Adds the object's **DDL** (DBMS_METADATA, without storage clauses) at the end, highlighted as SQL |
| `y` | Copies the DDL to the clipboard |
| `i` | Inserts the object's name at the cursor of the input area |
| `Ctrl-O` | Collects the catalog: object names, tables and columns (for completion and the red underlines), the kinds' counts, and the level on show again. Nothing is read before; after a DDL of yours the header says to collect again |

### 6.3 Monitor (Oracle, PostgreSQL)

What dbcli sends by itself (catalog reads for completion and underlines, the schema browser, the monitor's own queries, autotrace's helpers) starts with the comment `/* dbcli:bg */`, so it can be told from what you ran. The monitor leaves it out: a session busy with such a statement shows as idle, without its SQL_ID, and Top SQL skips it. Statements from before this mark existed may still show until they age out of the shared pool.

`Alt-4` opens it. **`Ctrl-O`** with the tab focused takes a snapshot; nothing is read otherwise (no refresh by itself). The header says when the snapshot was taken. The last snapshot is kept per connection (`monitor.json`), like the Schema tab: after a restart it is shown again, even before the connection is back, with its date when it is from an earlier day, until Ctrl-O takes a new one.

| Section | From | Shows |
|---|---|---|
| Sessions | V$SESSION | user sessions, active first: sid, user, program, what each is doing (its wait event, or `on cpu`), how long, its SQL_ID |
| Blocking | V$SESSION | each session waiting on another: the waiter, the blocker, the event, how long |
| Locked objects | V$LOCKED_OBJECT | which session holds which object in which mode |
| Top SQL | V$SQL | the 10 statements with the most elapsed time: runs, ms per run, buffer gets, rows, the text |
| Wait events | V$SYSTEM_EVENT | the non-idle waits since startup, most time first |

The user needs SELECT on `v_$session`, `v_$locked_object`, `v_$sql` and `v_$system_event` (granted as SYS); a section without its grant says which one is missing. PostgreSQL shows `pg_stat_activity` and `pg_locks`.

### Cells: editing and re-running earlier statements

Each record in the shell is an editable **cell** (like a notebook). The bottom cell is the new input.

**Connections.** A session holds up to four connections, `conn0` … `conn3`, and each cell runs on one of them, named in its prompt (`#c0 conn0> `: the cell's number from the top, then the connection in its own color). A new cell is on conn0; `Ctrl-0` … `Ctrl-3` in a cell move it to another (conn1-3 connect when first used, with the session's autocommit). Cells on different connections run at the same time; cells on one connection wait their turn, in the order started (`waiting for conn1…`), as one connection runs one statement at a time. Each connection is its own database session, so its own transaction: a `commit` commits only its connection's changes. conn0 also serves the schema browser, the monitor and completion. `F7` sets autocommit on all of them. A running cell shows a spinner where `>` goes; `Ctrl-C` on it cancels it.

```
 > select * from t where v > 100 or v = 1    ← moved up with ↑ and edited (background and prompt color)
 (2 rows)  (stale)                           ← edited, not re-run yet
 
 > select count(*) from t
 (1 row)
 
 > ▏                                         ← input cell
── Result · prod-ora · select * from t where v > 100 (2 rows) (stale) ──
```

- **↑ on the first line** of the input cell moves to the cell above; keep going up, or **↓ on the last line** to go down. Inside a multi-line cell ↑/↓ move between its lines. In NORMAL mode `k` / `j` do the same. Clicking a cell works too.
- The result pane shows the focused cell's result (and the Trace tab its trace). Back on the input cell it shows the latest run.
- An edited earlier cell gets **`(stale)`** under it and in the result title: what shows no longer matches its text.
- **Ctrl-O** in that cell re-runs it in place: the result and row count update and `(stale)` goes. Focus stays on the cell.
- **Ctrl-C** in an earlier cell drops the edits, back to the text that last ran; on a running cell it cancels the statement.
- **An emptied cell goes on one more delete**: delete all of a cell's text (`dd` on its one line, `ggdG`, `Shift+L`, backspace) and it stays, empty, its result gone with the text; one more delete key on the empty cell (backspace or Delete in INSERT mode, `dd`, `x`, `X`, `D` or `Shift+L` in NORMAL mode) removes the cell itself. The focus moves to the next cell; when the bottom cell goes, to the start of the cell above, which is the bottom one then (so a held backspace does not eat into it). The last cell left is only emptied, never removed.
- Text from the History or Schema tab goes to the cell edited last (a History recall goes to the input cell).

### Per database

| Database | Autocommit default | Notes |
|---|---|---|
| SQLite | on (each change commits) | `~` paths work; F7 cannot turn it off |
| PostgreSQL | on (`autocommit = false` turns it off; F7 toggles) | 10-second connect timeout |
| Oracle | **off**: run `commit;` yourself, or F7 | The trailing `;` is dropped (not for PL/SQL). CLOB/BLOB show as text / bytes. 10-second connect timeout |

> Oracle starts with autocommit off: quitting without a commit rolls changes back.

### 6.4 AI tab

`Alt-6` opens it. Ask about SQL and the answer streams into the tab, SQL highlighted behind a bar.

| Key | Does |
|---|---|
| `Enter` / `a` | Asks: type the question after the orange `prompt>` under the panel, Enter sends it (Esc drops it). `Alt-6` opens the tab with this line already open |
| `i` | Puts the last answer's last SQL block at the cursor of the input area (on a new line); `u` undoes |
| `y` | Copies the last answer |
| `c` | A new chat (the model forgets the earlier questions; the saved chat is emptied too) |
| `Ctrl-C` | Stops the answer being written |

- **Naming cells:** `@c0`, `@c1` … in a question (the numbers of the `#c0` prompts) show in bold blue as you type, and send **that cell's SQL** and its last outcome along: `why does @c1 fail?`, `join @c0 and @c2`. A number with no cell is refused before anything is sent. `a@c1` (inside a word) is not a reference.
- **What goes with each question:** the database type, the catalog `Ctrl-O` collected in the Schema tab (table and column names), and the SQL and last outcome (the result title, an error) of the cells the question names, or, naming none, of the cell you were in (or edited last). **Result rows are never sent.** The questions of the chat go along too, so "and sorted by name?" works.
- **The chat is kept** per connection in `.dbclidata/sessions/<connection>/ai.json`: the next `./dbcli` brings it back. Only the **latest 10** questions and answers are kept (on disk and in what goes to the model); older ones drop off as new ones come. A stopped or failed answer is shown but not sent again. Earlier answers go back to the model as their text only (without its thinking), which keeps the history valid however it was trimmed or restored.
- The model is Claude Opus 5.5 (`claude-opus-5-5`) at effort `medium`; a question the model declines is re-run on Anthropic's fallback model, and if that declines too the tab says so. `[ai]` in the config changes them (section 7).
- The key: the active subscription's (`./dbcli subscription add`, section 2), else `[ai] api_key`, else `ANTHROPIC_API_KEY`. **With none of these** the tab lists the saved subscriptions: Enter on one makes it the active one (saved as `[ai] subscription`). With no subscription at all it says to run `./dbcli subscription add` in a terminal; a subscription added or picked there shows in the tab without a restart. `a` still asks, for an `ant auth login` profile. It needs the Anthropic SDK: `.venv/bin/pip install -r requirements.txt` (or `.venv/bin/pip install anthropic`); without it the tab says so and the rest of dbcli works as before.
- Each question is billed to that key. The catalog is sent as a cached prefix, so repeated questions with the same catalog cost less.

## 7. Config file

Where: `.dbclidata/config.toml` in the repository (the `.dbclidata/` next to `./dbcli`).
Example: `config.example.toml` in the repository.

```toml
[ui]
panel = "history"               # first panel tab: history | schema | trace | monitor | bookmark | ai
show_panel = true               # false: start with the panel hidden (Ctrl-B shows it)
editing_mode = "vi"             # input area keys: vi (default) | emacs

[ai]                            # the AI tab (6.4); all optional
model = "claude-opus-5-5"
effort = "medium"               # low | medium | high | xhigh | max
# subscription = "work"         # the active [subscriptions.*] (./dbcli subscription set)

[subscriptions.work]            # ./dbcli subscription add writes these
api_key = "sk-ant-…"
# model = "claude-sonnet-5-5"   # this key's own model / effort, else [ai]'s

[templates]                     # called as @name args… (5.4); fills bookmarks.json once
a = "select * from t where num <= $1"

[abbreviations]                 # added to / overriding the built-in ones
s = "select "
cnt = "select count(*) from "

[connections.local]
type = "sqlite"
path = "~/data/local.db"

[connections.dev-pg]
type = "postgres"
host = "localhost"
port = 5432
dbname = "shop"
user = "app"
password = "app-password"

[connections.prod-ora]         # lowest = added last = the default
type = "oracle"
user = "scott"
dsn = "localhost:1521/FREEPDB1"   # host:port/service_name
password = "tiger"
# autocommit = true
```

| Key | sqlite | postgres | oracle |
|---|---|---|---|
| `type` | required | required | required |
| `path` | database file (memory if absent) | | |
| `host` `port` `dbname` `user` | | optional | `user` only |
| `dsn` | | connection string (`postgresql://…`) | `host:port/service` |
| `password` | | password (plain text) | the same |
| `password_env` | | an environment variable to read the password from instead | the same |
| `autocommit` | | default `true` | default `false` |

**Adding connections, and the default:** add a new connection at the bottom as `[connections.name]`. `./dbcli` with no name opens the lowest one; `./dbcli --list` shows them all. `[ui] panel = "connections"` from older configs opens History.

**Passwords:** credentials are stored in this file in plain text, so `.dbclidata/` is owner-only (folders 700, files 600) and in `.gitignore`. Use `password_env` for a connection whose password should not be in the file; without that variable set, the connection fails to connect.

## 8. Stored files

| File | Holds |
|---|---|
| `.dbclidata/config.toml` | Connections (passwords included) and settings (section 7) |
| `.dbclidata/sessions/<connection>/history` | That connection's queries (the History tab) |
| `.dbclidata/sessions/<connection>/snapshot.json` | F1's snapshot, read by the next start and then moved to `snapshot.prev.json` |
| `.dbclidata/sessions/<connection>/schema.json` | The Schema tab where it was left (kinds, a kind's objects, an object's details and DDL) and the catalog Ctrl-O collected, so both are back after a restart |
| `.dbclidata/sessions/<connection>/monitor.json` | The Monitor tab's last snapshot (and when it was taken), shown again after a restart until Ctrl-O takes a new one |
| `.dbclidata/sessions/<connection>/ai.json` | The AI tab's latest 10 questions and answers (with the context each one sent), brought back by the next start |
| `.dbclidata/sessions/<connection>/last.json` | That connection's last run: its text, outcome, result rows (up to 1000) and trace |
| `.dbclidata/bookmarks.json` | The bookmarks (id, query, optional memo and alias), edited in the Bookmark tab; those with an alias are the templates. Made from an older `templates.json` or `[templates]` on first start |
| `.dbclidata/words.db` | Word use counts for the gray suggestions, shared by all connections; saved on every run |

Delete `words.db` to reset the ranking, or a connection's folder to forget its history and last run.

## 9. Development

```bash
.venv/bin/python -m unittest          # unit tests
```

The source is in `src/dbcli/`; `./dbcli` at the repository root runs it with the `.venv` Python.

| Module | Does |
|---|---|
| `src/dbcli/__main__.py` | Command line: which connection to open |
| `src/dbcli/abbrev.py` | Finding and replacing `$abbreviations` |
| `src/dbcli/words.py` | The weighted word model, gray suggestions, saving what was learned |
| `src/dbcli/templates.py` | Bookmarks and the `@alias args` SQL templates |
| `src/dbcli/ai.py` | The AI tab: what is sent, the streamed answer, the SQL `i` inserts |
| `src/dbcli/subscription.py` | `./dbcli subscription list / add / set / delete / show / unset`: the AI tab's keys |
| `src/dbcli/connections.py` | `./dbcli connection list / add / delete / set` |
| `src/dbcli/tab.py` | Deciding what Tab does |
| `src/dbcli/panes.py` | Ctrl-W direction moves |
| `src/dbcli/db.py` | Database adapters (run, relation list, ping, autocommit, autotrace), statement cleanup |
| `src/dbcli/grid.py` | Drawing a result as a boxed table |
| `src/dbcli/trace.py` | Autotrace: statistics, SQL_ID, the text layout |
| `src/dbcli/trace_view.py` | The Trace tab's drawing: plan trees and colors |
| `src/dbcli/oracle_hints.py` | The Oracle hints offered by the completion |
| `src/dbcli/schema.py` | The schema browser's queries (kinds, objects, details, DDL) for each database |
| `src/dbcli/monitor.py` | The monitor's queries |
| `src/dbcli/formatter.py` | Ctrl-U formatting (sqlparse) |
| `src/dbcli/objects.py` | Finding the table, view and column names a statement uses (for the red underline) |
| `src/dbcli/store.py` | Per-connection files: history and the last run |
| `src/dbcli/config.py` | The config file and connection profiles |
| `src/dbcli/ui.py` | The screen, panel, session and keys |
