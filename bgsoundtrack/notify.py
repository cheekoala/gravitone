"""Say something when there is no terminal to say it in.

Launched from a desktop shortcut, a failure has nowhere to print: the window
never appears and it looks like clicking did nothing. These helpers put the
message on screen using whatever the desktop already has.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

_TK_DIALOG = r"""
import sys
import tkinter
import tkinter.messagebox as box

root = tkinter.Tk()
root.withdraw()
box.showerror(sys.argv[1], sys.argv[2])
root.destroy()
"""


def has_terminal() -> bool:
    try:
        return sys.stdout.isatty() or sys.stderr.isatty()
    except (AttributeError, ValueError):  # pragma: no cover - closed streams
        return False


def error(title: str, message: str) -> bool:
    """Show an error dialog. True if something was actually displayed."""
    for command in (
        ["kdialog", "--title", title, "--error", message],
        ["zenity", "--error", "--title", title, "--text", message],
        ["xmessage", "-center", f"{title}\n\n{message}"],
    ):
        if shutil.which(command[0]):
            try:
                subprocess.run(command, timeout=120, capture_output=True)
                return True
            except (OSError, subprocess.SubprocessError):
                continue
    try:
        done = subprocess.run(
            [sys.executable, "-c", _TK_DIALOG, title, message],
            timeout=120,
            capture_output=True,
        )
        return done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def complain(title: str, message: str) -> None:
    """Print it, and also show it if nobody is watching the terminal."""
    print(message, file=sys.stderr)
    if not has_terminal():
        error(title, message)
