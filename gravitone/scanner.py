"""The folder index: knowing what is in a folder without walking it again.

Scanning was happening on the request thread, so a poll from the UI could sit
behind an `rglob` of a few thousand files - which is what "the server goes
unresponsive" and "it takes a while to react" look like from the browser.

Now a single background worker owns all walking. Callers get whatever is
known right now, immediately, and a refresh is queued if it looks stale. The
index is written to disk, so a restart starts with answers instead of work.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

REFRESH_AFTER = 30.0      # seconds before a folder is worth walking again
CACHE_VERSION = 1


@dataclass
class Entry:
    files: list = field(default_factory=list)
    scanned: float = 0.0
    missing: bool = False


class Scanner:
    def __init__(self, is_audio, path: Path | None = None):
        self._is_audio = is_audio
        self.path = path
        self._lock = threading.Lock()
        self._index: dict = {}
        self._queue: queue.Queue = queue.Queue()
        self._queued: set = set()
        self._worker: threading.Thread | None = None
        self.version = 0        # bumped whenever the index changes
        self.pending = 0        # folders waiting to be walked
        self._dirty = False
        if path:
            self.load(path)

    # -- reading ---------------------------------------------------------

    def files(
        self, directory: Path, refresh: bool = True, block_if_unknown: bool = True
    ) -> list:
        """Audio files under `directory`, from the index.

        A folder nobody has ever indexed is walked here, once, because
        answering "nothing" would be a lie. Everything after that is served
        from the index and refreshed on the worker, so no repeat walk ever
        lands on a request thread.
        """
        key = str(directory)
        with self._lock:
            entry = self._index.get(key)
        if entry is None:
            if not block_if_unknown:
                if refresh:
                    self.request(directory)
                return []
            self._walk(directory)
            with self._lock:
                entry = self._index.get(key)
            return list(entry.files) if entry else []
        if refresh and time.time() - entry.scanned >= REFRESH_AFTER:
            self.request(directory)
        return list(entry.files)

    def known(self, directory: Path) -> bool:
        return str(directory) in self._index

    def count(self, directory: Path) -> int:
        return len(self.files(directory))

    # -- refreshing ------------------------------------------------------

    def request(self, directory: Path) -> None:
        """Ask for a folder to be walked, soon, on the worker."""
        key = str(directory)
        with self._lock:
            if key in self._queued:
                return
            self._queued.add(key)
            self.pending = len(self._queued)
        self._queue.put(directory)
        self._start()

    def _start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._run, daemon=True, name="gravitone-scan")
        self._worker.start()

    def _run(self) -> None:
        while True:
            try:
                directory = self._queue.get(timeout=5)
            except queue.Empty:
                self.save()
                return
            try:
                self._walk(directory)
            finally:
                with self._lock:
                    self._queued.discard(str(directory))
                    self.pending = len(self._queued)
                self._queue.task_done()

    def _walk(self, directory: Path) -> None:
        found: list = []
        missing = not directory.is_dir()
        if not missing:
            try:
                for path in sorted(directory.rglob("*")):
                    if path.name.startswith("."):
                        continue
                    try:
                        if path.is_file() and self._is_audio(path):
                            found.append(path)
                    except OSError:
                        continue
            except OSError:
                missing = True

        entry = Entry(files=found, scanned=time.time(), missing=missing)
        with self._lock:
            previous = self._index.get(str(directory))
            changed = previous is None or [str(p) for p in previous.files] != [
                str(p) for p in found
            ]
            self._index[str(directory)] = entry
            if changed:
                self.version += 1
                self._dirty = True

    def refresh_now(self, directory: Path) -> list:
        """Walk it here and now - for the CLI, which has nothing else to do."""
        self._walk(directory)
        return self.files(directory, refresh=False)

    def forget(self, directory: Path | None = None) -> None:
        with self._lock:
            if directory is None:
                self._index.clear()
            else:
                self._index.pop(str(directory), None)
            self._dirty = True

    def wait(self, timeout: float = 30.0) -> bool:
        """Block until the queue drains. Used by tests and the CLI."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if not self._queued:
                    return True
            time.sleep(0.02)
        return False

    # -- persistence -----------------------------------------------------

    def load(self, path: Path | None = None) -> None:
        path = path or self.path
        if not path:
            return
        self.path = path
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if raw.get("version") != CACHE_VERSION:
            return
        with self._lock:
            for key, item in (raw.get("folders") or {}).items():
                self._index[key] = Entry(
                    files=[Path(p) for p in item.get("files", [])],
                    scanned=float(item.get("scanned", 0)),
                    missing=bool(item.get("missing")),
                )

    def save(self, path: Path | None = None) -> None:
        path = path or self.path
        if not path:
            return
        with self._lock:
            if not self._dirty:
                return
            payload = {
                "version": CACHE_VERSION,
                "folders": {
                    key: {
                        "files": [str(p) for p in entry.files],
                        "scanned": entry.scanned,
                        "missing": entry.missing,
                    }
                    for key, entry in self._index.items()
                },
            }
            self._dirty = False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            pass
