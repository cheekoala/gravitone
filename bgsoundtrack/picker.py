"""Open the operating system's own folder/file chooser.

The UI runs in a browser, and a browser's file input hands over file contents,
never paths - useless for linking. So when the machine running `bgst ui` has
Tk available (it ships with most Python builds), we open the real chooser
there and send back the path that was picked.

It runs in a subprocess: Tk insists on owning a thread's main loop, and a
crashing toolkit should never take the server down with it.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass

_SCRIPT = r"""
import sys
import tkinter
import tkinter.filedialog as dialog

root = tkinter.Tk()
root.withdraw()
try:
    root.attributes("-topmost", True)
except tkinter.TclError:
    pass

kind, title = sys.argv[1], sys.argv[2]
if kind == "folder":
    picked = dialog.askdirectory(title=title, mustexist=True)
    paths = [picked] if picked else []
else:
    paths = list(dialog.askopenfilenames(title=title))

root.destroy()
for path in paths:
    print(path)
"""


@dataclass(frozen=True)
class PickResult:
    paths: list[str]
    available: bool = True
    reason: str | None = None


def available() -> bool:
    """Can we show a native dialog on this machine right now?"""
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return False
    probe = subprocess.run(
        [sys.executable, "-c", "import tkinter; tkinter.Tk().destroy()"],
        capture_output=True,
        timeout=20,
    )
    return probe.returncode == 0


def pick(kind: str = "folder", title: str = "Choose a folder", timeout: float = 600) -> PickResult:
    """Show the chooser and wait for the person at that machine."""
    if kind not in ("folder", "files"):
        raise ValueError(f"unknown picker kind: {kind}")
    try:
        done = subprocess.run(
            [sys.executable, "-c", _SCRIPT, kind, title],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return PickResult([], available=True, reason="the chooser timed out")
    except OSError as exc:
        return PickResult([], available=False, reason=str(exc))

    if done.returncode != 0:
        reason = (done.stderr or "").strip().splitlines()[-1:] or ["no display or no Tk"]
        return PickResult([], available=False, reason=reason[0])
    return PickResult([line for line in done.stdout.splitlines() if line.strip()])
