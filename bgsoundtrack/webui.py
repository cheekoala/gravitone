"""A tiny local web UI, served from the standard library.

Why a browser and not a desktop toolkit: it is the only GUI that is already
installed on every platform, needs no build step and no dependencies, and
happens to double as a phone remote when you run it with --host 0.0.0.0.

The server binds to localhost by default. Every /api call needs the session
token, which is printed in the URL, so a random page in another tab cannot
drive your player (or browse your disk).
"""

from __future__ import annotations

import errno
import json
import mimetypes
import os
import secrets
import signal
import socket
import sys
import time
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bgsoundtrack import (
    __version__,
    config as config_module,
    instance as instance_state,
    library,
    notify,
    player,
    playlists,
)
from bgsoundtrack.service import Session

UI_DIR = Path(__file__).parent / "ui"

# Folders offered as starting points in the "add music" browser.
MUSIC_HINTS = ("Music", "Musique", "Musik", "Muziek", "音楽", "Downloads", "Desktop")


def _audio_count(directory: Path) -> int:
    try:
        return sum(1 for entry in directory.iterdir() if library.is_audio(entry))
    except OSError:
        return 0


def browse(path: str | None) -> dict:
    """Directory listing for the built-in file picker.

    Linking needs real filesystem paths, which a browser file input will never
    hand us, so the picker is served from this side instead.
    """
    home = Path.home()
    target = Path(path).expanduser() if path else home
    try:
        target = target.resolve()
        entries = sorted(target.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:
        raise ValueError(f"cannot open {target}: {exc}") from exc

    directories, files = [], []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            if entry.is_dir():
                directories.append({"name": entry.name, "path": str(entry),
                                    "audio": _audio_count(entry)})
            elif library.is_audio(entry):
                files.append({"name": entry.name, "path": str(entry)})
        except OSError:
            continue

    shortcuts = [{"name": "Home", "path": str(home)}]
    for hint in MUSIC_HINTS:
        candidate = home / hint
        if candidate.is_dir():
            shortcuts.append({"name": hint, "path": str(candidate)})
    return {
        "path": str(target),
        "parent": None if target.parent == target else str(target.parent),
        "dirs": directories,
        "files": files,
        "shortcuts": shortcuts,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = f"bgst/{__version__}"
    session: Session
    token: str

    # -- plumbing --------------------------------------------------------

    def log_message(self, *args) -> None:  # quiet by default
        if getattr(self.server, "verbose", False):
            super().log_message(*args)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, status: int = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-BGST-Token", "")
        return secrets.compare_digest(supplied, self.token)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc

    # -- routes ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        url = urlparse(self.path)
        if url.path.startswith("/api/"):
            self._api(url, method="GET")
            return
        self._static(url.path)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        self._api(urlparse(self.path), method="POST")

    def _static(self, path: str) -> None:
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        candidate = (UI_DIR / name).resolve()
        if UI_DIR.resolve() not in candidate.parents or not candidate.is_file():
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self._send(HTTPStatus.OK, candidate.read_bytes(), content_type)

    def _api(self, url, method: str) -> None:
        if not self._authorized():
            self._json({"error": "bad or missing token"}, HTTPStatus.FORBIDDEN)
            return
        route = url.path[len("/api/"):]
        try:
            payload = self._dispatch(route, method, parse_qs(url.query))
        except ValueError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except KeyError as exc:
            self._json({"error": f"unknown setting {exc}"}, HTTPStatus.BAD_REQUEST)
            return
        except library.LibraryError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except playlists.PlaylistError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except player.PlaybackError as exc:
            self._json({"error": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if payload is None:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        self._json(payload)

    def _dispatch(self, route: str, method: str, query: dict):
        session = self.session
        if route == "state" and method == "GET":
            return session.snapshot()
        if route == "browse" and method == "GET":
            return browse((query.get("path") or [None])[0])
        if route == "library" and method == "GET":
            section = (query.get("section") or ["music"])[0]
            if section not in library.SECTIONS:
                raise ValueError(f"unknown section {section!r}")
            return session.library(section)
        if method != "POST":
            return None
        body = self._body()
        if route == "play":
            session.start()
        elif route == "stop":
            session.stop()
        elif route == "toggle":
            session.toggle()
        elif route == "skip":
            session.skip()
        elif route == "ban":
            result = session.ban(body.get("name"))
            return {**session.snapshot(), "result": result}
        elif route == "config":
            live = session.update_config(body)
            return {**session.snapshot(), "live": live}
        elif route == "link":
            path = body.get("path")
            if not path:
                raise ValueError("link needs a path")
            result = session.link(
                path, body.get("section", "music"), bool(body.get("relative"))
            )
            return {**session.snapshot(), "result": result}
        elif route == "unlink":
            session.unlink(body["name"], body.get("section", "music"))
        elif route == "remove-track":
            name = body.get("name")
            if not name:
                raise ValueError("remove-track needs a name")
            result = session.remove_track(name, body.get("section", "music"))
            return {**session.snapshot(), "result": result}
        elif route == "restore-track":
            target = body.get("target")
            if not target:
                raise ValueError("restore-track needs a target")
            result = session.restore_track(target)
            return {**session.snapshot(), "result": result}
        elif route == "playlist":
            action = body.get("action", "select")
            if action == "select":
                result = session.select_playlist(body["id"])
            elif action == "new":
                result = session.add_playlist(body.get("name", ""), body.get("source"))
            elif action == "rename":
                result = session.rename_playlist(body["id"], body.get("name", ""))
            elif action == "remove":
                result = session.remove_playlist(body["id"])
            else:
                raise ValueError(f"unknown playlist action {action!r}")
            return {**session.snapshot(), "result": result}
        elif route == "prune":
            result = session.prune()
            return {**session.snapshot(), "result": result}
        elif route == "source":
            path = body.get("path")
            if not path:
                raise ValueError("source needs a path")
            section = body.get("section", "music")
            if body.get("remove"):
                result = session.remove_source(path, section)
            else:
                result = session.add_source(path, section)
            return {**session.snapshot(), "result": result}
        elif route == "pick":
            # Blocks until the person at the machine answers the dialog.
            picked = session.pick(
                body.get("kind", "folder"),
                body.get("title", "Choose a folder for bgst"),
            )
            return {
                "paths": picked.paths,
                "available": picked.available,
                "reason": picked.reason,
            }
        else:
            return None
        return session.snapshot()


def _lan_address() -> str | None:
    """Best guess at this machine's LAN IP, for the phone-remote hint."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1: routed nowhere, sends nothing
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


DEFAULT_PORT = 8765
PORT_TRIES = 12


class PortInUse(OSError):
    """The port asked for is taken, and we were told not to wander."""


def _bind(host: str, port: int, handler, fallback: bool) -> ThreadingHTTPServer:
    """Bind the port, or the next free one when we are allowed to move.

    A port left busy by an earlier run used to surface as a bare
    'Address already in use' traceback, which from a desktop shortcut meant
    a window that never appeared.
    """
    last: OSError | None = None
    for offset in range(PORT_TRIES if fallback else 1):
        try:
            return ThreadingHTTPServer((host, port + offset), handler)
        except OSError as exc:
            if exc.errno not in (errno.EADDRINUSE, errno.EACCES):
                raise
            last = exc
    if not fallback:
        raise PortInUse(
            f"port {port} is already in use - something else (an older bgst?) "
            f"is listening there"
        ) from last
    # Everything in the range is taken: let the OS pick anything free.
    try:
        return ThreadingHTTPServer((host, 0), handler)
    except OSError as exc:
        raise PortInUse(f"could not open a port on {host}: {exc}") from exc


def serve(
    session: Session,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    token: str | None = None,
    open_browser: bool = True,
    verbose: bool = False,
    fallback: bool = True,
) -> ThreadingHTTPServer:
    """Start the UI server. Returns it running on a background thread."""
    token = token or secrets.token_urlsafe(16)
    handler = type("BoundHandler", (Handler,), {"session": session, "token": token})
    httpd = _bind(host, port, handler, fallback)
    httpd.verbose = verbose
    httpd.daemon_threads = True
    httpd.token = token

    shown_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = f"http://{shown_host}:{httpd.server_port}/#{token}"
    httpd.url = url
    threading.Thread(target=httpd.serve_forever, daemon=True, name="bgst-ui").start()
    httpd.opened = webbrowser.open(url) if open_browser else False
    return httpd


def run(
    config_path: Path | None = None,
    host: str = "127.0.0.1",
    port: int | None = None,
    open_browser: bool = True,
    root: str | None = None,
    new: bool = False,
) -> int:
    """Blocking entry point used by `bgst ui`."""
    wanted = DEFAULT_PORT if port is None else port

    # Already running? Open that one instead of fighting it for the port.
    if not new:
        existing = instance_state.running(config_path)
        if existing is not None:
            print(f"bgst ui is already running  {existing.url}", flush=True)
            if open_browser and not webbrowser.open(existing.url):
                notify.complain(
                    "bgst is already running",
                    f"bgst is already running. Go to:\n\n{existing.url}",
                )
            print("use 'bgst ui --new' for a second one, or 'bgst ui --stop' to stop it")
            return 0

    config = config_module.load(config_path)
    if root:
        config.root = root
    config.validate()
    library.init(config)
    session = Session(config, config_path)

    try:
        httpd = serve(
            session,
            host=host,
            port=wanted,
            open_browser=open_browser,
            fallback=port is None,
        )
    except PortInUse as exc:
        notify.complain(
            "bgst could not start",
            f"{exc}.\n\nTry 'bgst ui' without --port, or 'bgst ui --stop' to stop "
            f"the one that is running.",
        )
        return 1
    except OSError as exc:
        notify.complain("bgst could not start", f"could not start the UI: {exc}")
        return 1

    instance_state.write(
        instance_state.Instance(
            pid=os.getpid(),
            host=host,
            port=httpd.server_port,
            url=httpd.url,
            token=httpd.token,
            started=time.time(),
        ),
        config_path,
    )

    if httpd.server_port != wanted:
        print(f"port {wanted} was busy, using {httpd.server_port} instead", flush=True)
    print(f"bgst ui  {httpd.url}", flush=True)
    if open_browser and not httpd.opened:
        # Launched from a shortcut with no browser handler, this is the
        # difference between a link and a click that seems to do nothing.
        notify.complain(
            "bgst is running",
            f"bgst is running, but no browser opened. Go to:\n\n{httpd.url}",
        )
    if host in ("0.0.0.0", "::", ""):
        lan = _lan_address()
        if lan:
            print(f"phone    http://{lan}:{httpd.server_port}/#{httpd.token}")
        print("shared on your network - anyone with the link can control playback")
    print("Ctrl-C to quit", flush=True)

    stopping = threading.Event()
    for name in ("SIGTERM", "SIGINT"):
        handler = getattr(signal, name, None)
        if handler is not None:
            try:
                signal.signal(handler, lambda *_: stopping.set())
            except ValueError:  # pragma: no cover - not the main thread
                pass
    try:
        stopping.wait()
    except KeyboardInterrupt:  # pragma: no cover - belt and braces
        pass
    finally:
        print("\nstopping")
        session.stop()
        httpd.shutdown()
        instance_state.clear(config_path)
    return 0


def stop(config_path: Path | None = None) -> int:
    """`bgst ui --stop`: end the running UI, if there is one."""
    existing = instance_state.running(config_path)
    if existing is None:
        print("no bgst ui is running")
        return 0
    if instance_state.stop(existing):
        instance_state.clear(config_path)
        print(f"stopped bgst ui on port {existing.port}")
        return 0
    print(f"could not stop the bgst ui on port {existing.port}", file=sys.stderr)
    return 1
