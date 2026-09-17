"""Knowing whether a gravitone UI is already running, and where.

Clicking the shortcut twice, or starting the UI from the installer and then
from the menu, used to mean a second server fighting for the same port. The
running instance writes down where it is; the next one reads that and just
opens the browser at it.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from gravitone.config import config_path

# When this process started. Anything installed after it is code we are not
# running - see is_stale().
PROCESS_START = time.time()


def state_path(config_file: Path | None = None) -> Path:
    """Where the running UI writes down where it is.

    Named after the config it belongs to, so two configs living in one folder
    do not adopt each other's server.
    """
    base = config_file or config_path()
    if base.stem == "config":
        return base.parent / "ui.json"
    return base.parent / f"{base.stem}.ui.json"


@dataclass(frozen=True)
class Instance:
    pid: int
    host: str
    port: int
    url: str
    token: str
    started: float = 0.0

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "host": self.host,
            "port": self.port,
            "url": self.url,
            "token": self.token,
            "started": self.started,
        }


def write(instance: Instance, config_file: Path | None = None) -> Path:
    path = state_path(config_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(instance.to_dict(), indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)  # it holds the session token
    except OSError:  # pragma: no cover - exotic filesystems
        pass
    return path


def clear(config_file: Path | None = None) -> None:
    try:
        state_path(config_file).unlink()
    except OSError:
        pass


def read(config_file: Path | None = None) -> Instance | None:
    path = state_path(config_file)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return Instance(
            pid=int(raw["pid"]),
            host=str(raw.get("host", "127.0.0.1")),
            port=int(raw["port"]),
            url=str(raw["url"]),
            token=str(raw.get("token", "")),
            started=float(raw.get("started", 0)),
        )
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return None


def package_mtime() -> float:
    """When the installed gravitone was last written to.

    Upgrading while the UI is running leaves a process serving the new page
    from disk with the old Python in memory - which shows up as the browser
    asking for settings the server has never heard of.
    """
    root = Path(__file__).parent
    newest = 0.0
    candidates = list(root.glob("*.py")) + list((root / "ui").glob("*"))
    # pip stamps the dist-info directory at install time, which catches an
    # upgrade even when the files inside kept their original timestamps.
    for sibling in root.parent.glob("gravitone*.dist-info"):
        candidates.append(sibling)
        candidates.append(sibling / "RECORD")
    for path in candidates:
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


_staleness: tuple = (0.0, False)


def forget_staleness() -> None:
    """Drop the cached answer - after an install, or in a test."""
    global _staleness
    _staleness = (0.0, False)


def process_is_stale(ttl: float = 10.0) -> bool:
    """True if gravitone was updated on disk after this process started.

    Cached: this is asked once a second and answers by stat()ing the package.
    """
    global _staleness
    now = time.monotonic()
    if now - _staleness[0] < ttl:
        return _staleness[1]
    value = package_mtime() > PROCESS_START + 1
    _staleness = (now, value)
    return value


def is_stale(instance: Instance) -> bool:
    """True if that running instance predates the installed code."""
    return bool(instance.started) and package_mtime() > instance.started + 1


def token_path(config_file: Path | None = None) -> Path:
    base = config_file or config_path()
    return base.parent / "token"


def token(config_file: Path | None = None, fresh: bool = False) -> str:
    """This install's UI token, kept between runs.

    A token that changed on every start meant every image URL changed too,
    so a restart threw away the browser's whole cache of covers - and any
    open page needed the new link.
    """
    path = token_path(config_file)
    if not fresh:
        try:
            saved = path.read_text(encoding="utf-8").strip()
            if saved:
                return saved
        except OSError:
            pass
    made = secrets.token_urlsafe(16)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(made + "\n", encoding="utf-8")
        path.chmod(0o600)
    except OSError:
        pass
    return made


def alive(instance: Instance) -> bool:
    """Is that instance still there? Checks the process and the port."""
    if instance.pid and instance.pid != os.getpid():
        try:
            os.kill(instance.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:  # someone else's process reused the pid
            return False
        except OSError:
            pass
    return port_busy(instance.host, instance.port)


def port_busy(host: str, port: int) -> bool:
    """True if something is already listening there."""
    target = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex((target, port)) == 0


def running(config_file: Path | None = None) -> Instance | None:
    """The live instance, if there is one. Clears the note if it is stale."""
    instance = read(config_file)
    if instance is None:
        return None
    if instance.pid == os.getpid():
        return None
    if alive(instance):
        return instance
    clear(config_file)
    return None


def stop(instance: Instance, timeout: float = 5.0) -> bool:
    """Ask a running UI to quit. True once it is gone."""
    try:
        os.kill(instance.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return not port_busy(instance.host, instance.port)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not port_busy(instance.host, instance.port):
            return True
        time.sleep(0.2)
    return not port_busy(instance.host, instance.port)
