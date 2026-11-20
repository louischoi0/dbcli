# dotfiles

The Claude Code and tmux settings this project is worked on with: a copy, for setting up
another machine. Nothing here is read by dbcli.

| File | Goes to |
|---|---|
| `claude/settings.json` | `~/.claude/settings.json`: model, effort per model, theme |
| `claude/statusline-tmux.sh` | `~/.claude/statusline-tmux.sh`: puts a Claude session's model, effort, context and limits on its tmux pane (`@claude_info`) |
| `claude/skills/usage-band/` | `~/.claude/skills/usage-band/`: the plugin that runs `statusline-tmux.sh` (its `.claude-plugin/types/` is generated, so not kept here) |
| `tmux/tmux.conf` | `~/.tmux.conf`: true color, the navy status bar with the Claude info, extended keys (dbcli's Ctrl+Enter), Ctrl+<Korean jamo> as Ctrl+<letter> |

```bash
cp dotfiles/claude/settings.json dotfiles/claude/statusline-tmux.sh ~/.claude/
cp -r dotfiles/claude/skills/usage-band ~/.claude/skills/
cp dotfiles/tmux/tmux.conf ~/.tmux.conf && tmux source-file ~/.tmux.conf
```

No credentials are kept here: `~/.claude/.credentials.json`, the history and the sessions stay
on the machine.
