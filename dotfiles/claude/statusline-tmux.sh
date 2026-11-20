#!/bin/bash
# Hands a Claude Code session's info to tmux: the text is stored on the
# session's pane as @claude_info, and ~/.tmux.conf shows the active pane's
# @claude_info in status-right.
#
# Run by the usage-band plugin (~/.claude/skills/usage-band), which writes the
# JSON a status line command is given to stdin. It is not set as "statusLine"
# in settings.json: Claude Code keeps a row under the prompt for a status line
# even when the command prints nothing.
#
# The text carries tmux styles (#[...]): the model in bold on the bar's own navy, a lighter
# block for the effort, then one gauge each for context, 5-hour limit and 7-day limit.
# A gauge is its own label: " ctx 42% " with the left 42% of its cells filled. The
# fill is sky blue below 70%, orange from 70%, red and bold from 90%. Colors go with the bar
# color set in ~/.tmux.conf (status-style).

info=$(jq -r '
  def gauge($label; $pct):
    ($pct | floor) as $p
    | " \($label) \(if $p < 10 then " " else "" end)\($p)% " as $t
    | ($t | length) as $n
    | ($p * $n / 100 | round | if . > $n then $n elif . < 0 then 0 else . end) as $k
    | (if $p >= 90 then "bg=#d55e00,fg=#ffffff,bold"
       elif $p >= 70 then "bg=#e69f00,fg=#000000,nobold"
       else "bg=#8cc8ec,fg=#000000,nobold" end) as $fill
    | "#[\($fill)]\($t[:$k])#[bg=#2f6f95,fg=#ffffff,nobold]\($t[$k:])#[default]";

  [
    ([
      (.model.display_name // empty | gsub("#"; "") | "#[bg=#0b3954,fg=#ffffff,bold] \(.) "),
      (.effort.level // empty | gsub("#"; "") | "#[bg=#2f6f95,fg=#ffffff,nobold] \(.) ")
    ] | join("") | select(. != "") | . + "#[default]"),
    (.context_window.used_percentage | select(. != null) | gauge("ctx"; .)),
    (.rate_limits.five_hour.used_percentage | select(. != null) | gauge("5h"; .)),
    (.rate_limits.seven_day.used_percentage | select(. != null) | gauge("7d"; .))
  ] | join(" ")
' 2>/dev/null)

[ -n "$TMUX_PANE" ] && [ -n "$info" ] || exit 0
tmux set-option -p -t "$TMUX_PANE" @claude_info "$info" 2>/dev/null
tmux refresh-client -S 2>/dev/null
exit 0
