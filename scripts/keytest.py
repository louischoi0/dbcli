"""Show the raw bytes your terminal sends for each key (to debug Ctrl+Enter).

    .venv/bin/python scripts/keytest.py        # press keys, `q` to quit

Ctrl+Enter works in dbcli when it shows up as one of:
    \\x1b[27;5;13~   (xterm modifyOtherKeys)      \\x1b[13;5u   (CSI u)
If it shows '\\r' (same as Enter), the terminal or tmux is not passing it through.
"""
import os
import select
import sys
import termios
import tty

KNOWN = {
    "\x1b[27;5;13~": "Ctrl+Enter (xterm)  -> dbcli: run",
    "\x1b[13;5u": "Ctrl+Enter (CSI u)  -> dbcli: run",
    "\x1b\r": "Alt+Enter           -> dbcli: run",
    "\x1b[15~": "F5                  -> dbcli: run",
    "\r": "Enter (or a Ctrl+Enter the terminal does not tell apart)",
    "\n": "Ctrl-J              -> dbcli: next cell (vim mode), run (emacs mode)",
    "\x0b": "Ctrl-K              -> dbcli: previous cell (vim mode)",
    "\x0f": "Ctrl-O              -> dbcli: run (also what Ctrl+Shift+O sends when not told apart)",
    "\x1b[27;6;79~": "Ctrl+Shift+O (xterm) -> dbcli: run every statement (script)",
    "\x1b[27;6;111~": "Ctrl+Shift+O (xterm) -> dbcli: run every statement (script)",
    "\x1b[27;5;79~": "Ctrl+Shift+O (tmux)  -> dbcli: run every statement (script)",
    "\x1b[79;6u": "Ctrl+Shift+O (CSI u) -> dbcli: run every statement (script)",
    "\x1b[111;6u": "Ctrl+Shift+O (CSI u) -> dbcli: run every statement (script)",
    "\x1b[79;5u": "Ctrl+Shift+O (CSI u) -> dbcli: run every statement (script)",
}


def main() -> None:
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    sys.stdout.write("\x1b[>4;1m")  # ask for distinct modified keys, like dbcli does
    sys.stdout.write("Press keys (q: quit)\r\n")
    sys.stdout.flush()
    try:
        tty.setraw(fd)
        while True:
            data = os.read(fd, 64)
            while select.select([fd], [], [], 0.05)[0]:  # collect the rest of a sequence
                data += os.read(fd, 64)
            text = data.decode(errors="replace")
            if text == "q":
                break
            note = KNOWN.get(text, "")
            sys.stdout.write(f"{text!r:24} {note}\r\n")
            sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\x1b[>4;0m")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
