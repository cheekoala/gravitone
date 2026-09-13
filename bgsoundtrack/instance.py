"""Knowing whether a bgst UI is already running, and where.

Clicking the shortcut twice, or starting the UI from the installer and then
from the menu, used to mean a second server fighting for the same port. The
running instance writes down where it is; the next one reads that and just
opens the browser at it.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from bgsoundtrack.config import config_path


def state_path(config_file: Path | None = None) -> Path:
    base = config_file or config_path()
    return base.parent / "ui.json"


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
