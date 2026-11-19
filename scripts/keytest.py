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
    "\n": "Ctrl-J              -> dbcli: run",
    "\x0f": "Ctrl-O              -> dbcli: run",
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
