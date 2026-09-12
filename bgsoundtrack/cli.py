"""Command line interface: bgst <command>."""

from __future__ import annotations

import argparse
import os
import random
import signal
import sys
import threading
from pathlib import Path

from bgsoundtrack import (
    __version__,
    config as config_module,
    engine,
    library,
    player,
    playlists,
)
from bgsoundtrack.config import Config


def _fmt(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}:{secs:02d}" if minutes else f"{secs}s"


def _config_path(args) -> Path | None:
    return Path(args.config).expanduser() if args.config else None


def _load_config(args) -> Config:
    config = config_module.load(_config_path(args))
    if args.root:
        config.root = str(Path(args.root).expanduser())
    config.validate()
    return config


def _load(args):
    """Config plus the playlist store, and whichever playlist is selected."""
    config = _load_config(args)
    store = playlists.load(config, _config_path(args))
    wanted = getattr(args, "playlist", None)
    # --playlist is a one-off override; it must not change what is selected.
    playlist = store.get(wanted) if wanted else store.current()
    return config, store, playlist


# -- commands ------------------------------------------------------------


def cmd_init(args) -> int:
    config, store, playlist = _load(args)
    created = library.init(config, playlist)
    print(f"custom soundtrack folder: {config.root_path}")
    for path in created:
        print(f"  created {path}")
    if not created:
        print("  already set up")
    print("\nAdd music with:   bgst link ~/Music/some-album")
    print("Add ambience with: bgst link --ambient ~/Sounds/rain.ogg")
    return 0


def cmd_link(args) -> int:
    config, store, playlist = _load(args)
    library.init(config, playlist)
    section = "ambient" if args.ambient else "music"
    result = library.link(
        config,
        [Path(p) for p in args.paths],
        section=section,
        recursive=not args.no_recursive,
        relative=args.relative,
        playlist=playlist,
    )
    for path in result.linked:
        print(f"linked {path.name}")
    for path, reason in result.skipped:
        print(f"skipped {path.name}: {reason}", file=sys.stderr)
    print(
        f"\n{len(result.linked)} linked into {playlist.name}/{section}, "
        f"{len(result.skipped)} skipped"
    )
    return 0


def cmd_unlink(args) -> int:
    config, store, playlist = _load(args)
    section = "ambient" if args.ambient else "music"
    for name in args.names:
        how, target = library.remove_track(config, name, section=section, playlist=playlist)
        store.save()
        print(f"{how} {name}" + ("" if how == "unlinked" else f" (remembered for {playlist.name})"))
    return 0


def cmd_playlist(args) -> int:
    """Switch between sets of music, each with its own links and removals."""
    config, store, playlist = _load(args)

    if args.action == "list":
        for item in store.playlists:
            mark = "*" if item.id == store.active else " "
            counts = library.entries(config, "music", item)
            print(
                f" {mark} {item.name}  [{item.id}]  {len(counts)} music, "
                f"{len(item.music_sources) + len(item.ambient_sources)} source(s), "
                f"{len(item.excluded)} removed"
            )
        return 0

    if args.action == "use":
        chosen = store.select(args.name)
        library.init(config, chosen)
        store.save()
        print(f"now playing from {chosen.name}")
        return 0

    if args.action == "new":
        created = store.add(args.name)
        library.init(config, created)
        if args.source:
            library.add_source(config, Path(args.source), playlist=created)
        if args.use:
            store.select(created.id)
        store.save()
        print(f"created playlist {created.name} [{created.id}]")
        if args.source:
            print(f"  source {Path(args.source).expanduser().resolve()}")
        if args.use:
            print("  selected")
        return 0

    if args.action == "rename":
        renamed = store.rename(args.name, args.new_name)
        store.save()
        print(f"renamed to {renamed.name}")
        return 0

    if args.action == "remove":
        gone = store.remove(args.name)
        store.save()
        print(f"removed playlist {gone.name} (its links are still in {config.root_path})")
        return 0

    if args.action == "removed":
        if not playlist.excluded:
            print(f"nothing removed from {playlist.name}")
            return 0
        print(f"removed from {playlist.name}:")
        for target in playlist.excluded:
            print(f"  {target}")
        return 0

    if args.action == "restore":
        if args.all:
            count = len(playlist.excluded)
            playlist.excluded = []
            library.invalidate_cache()
            store.save()
            print(f"restored {count} track(s) to {playlist.name}")
            return 0
        for target in args.paths:
            restored = library.restore_track(config, target, playlist=playlist)
            print(f"restored {restored.name}")
        store.save()
        return 0

    return 0


def cmd_source(args) -> int:
    """Play a folder in place, instead of linking its files one by one."""
    config, store, playlist = _load(args)
    section = "ambient" if getattr(args, "ambient", False) else "music"

    if args.action == "list":
        print(f"playlist: {playlist.name}\n")
        for name in ("music", "ambient"):
            folders = playlist.sources(name)
            print(f"{name} sources ({len(folders)}):")
            for folder in folders:
                mark = "" if folder.is_dir() else "  MISSING"
                print(f"  {folder}  [{len(library.scan(folder))} audio]{mark}")
            print()
        return 0

    for target in args.paths:
        if args.action == "add":
            added = library.add_source(config, Path(target), section=section, playlist=playlist)
            print(
                f"added {section} source {added} to {playlist.name} "
                f"({len(library.scan(added))} audio files)"
            )
        else:
            removed = library.remove_source(
                config, Path(target), section=section, playlist=playlist
            )
            print(f"removed {section} source {removed} from {playlist.name}")
    store.save()
    return 0


def cmd_list(args) -> int:
    config, store, playlist = _load(args)
    sections = ("ambient",) if args.ambient else ("music",) if args.music else library.SECTIONS
    print(f"playlist: {playlist.name}\n")
    for section in sections:
        entries = library.entries(config, section, playlist)
        print(f"{section} ({len(entries)}):")
        for entry in entries:
            tag = "" if entry.origin == "link" else "  (source)"
            if args.targets:
                print(f"  {entry.name} -> {entry.target}{tag}")
            else:
                print(f"  {entry.name}{tag}")
        for entry in library.broken(config, section, playlist):
            print(f"  {entry.name} -> BROKEN", file=sys.stderr)
        print()
    if playlist.excluded:
        print(f"removed from {playlist.name} ({len(playlist.excluded)}):")
        for target in playlist.excluded:
            print(f"  {Path(target).name}")
        print()
    return 0


def cmd_prune(args) -> int:
    config, store, playlist = _load(args)
    removed = library.prune(config, playlist=playlist)
    for path in removed:
        print(f"removed broken link {path.name}")
    print(f"{len(removed)} broken link(s) removed")
    return 0


def cmd_config(args) -> int:
    path = Path(args.config).expanduser() if args.config else config_module.config_path()
    config = _load_config(args)
    if args.assignments:
        for assignment in args.assignments:
            if "=" not in assignment:
                print(f"expected key=value, got {assignment!r}", file=sys.stderr)
                return 2
            key, value = assignment.split("=", 1)
            key = key.strip()
            try:
                config_module.set_value(config, key, value.strip())
            except KeyError:
                print(
                    f"unknown setting {key!r}. Known: {', '.join(config_module.known_keys())}",
                    file=sys.stderr,
                )
                return 2
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
        saved = config_module.save(config, path)
        print(f"saved {saved}")
    for key, value in config.to_dict().items():
        print(f"{key} = {value}")
    return 0


def cmd_doctor(args) -> int:
    config, store, playlist = _load(args)
    print(f"version         {__version__}")
    print(f"root            {config.root_path}")
    print(f"root exists     {config.root_path.is_dir()}")
    print(f"playlist        {playlist.name} ({len(store.playlists)} in total)")
    for section in library.SECTIONS:
        entries = library.entries(config, section, playlist)
        linked = sum(1 for entry in entries if entry.origin == "link")
        bad = library.broken(config, section, playlist)
        print(
            f"{section:15} {len(entries)} playable "
            f"({linked} linked, {len(entries) - linked} from sources), "
            f"{len(bad)} broken link(s)"
        )
    for section in library.SECTIONS:
        for folder in playlist.sources(section):
            state = "ok" if folder.is_dir() else "MISSING"
            print(f"{section[:7]} source  {folder} [{state}]")
    from bgsoundtrack import picker

    found = player.available()
    print(f"players found   {', '.join(found) if found else 'NONE'}")
    chooser = picker.available()
    print(f"file chooser    {'yes' if chooser else 'no (Tk missing or no display)'}")
    if not chooser:
        print(
            "                the UI falls back to its own file browser; "
            "for the system dialog install Tk (Debian/Ubuntu: sudo apt install python3-tk)"
        )
    if not found:
        print(
            "\nInstall ffmpeg (for ffplay), mpv, or vlc to play audio.",
            file=sys.stderr,
        )
        return 1
    return 0


def _apply_play_overrides(config: Config, args) -> None:
    for name in ("gap_min", "gap_max", "ambient_chance", "volume", "ambient_volume"):
        value = getattr(args, name)
        if value is not None:
            setattr(config, name, value)
    if args.no_shuffle:
        config.shuffle = False
    if args.no_loop:
        config.loop = False
    if args.no_ambient:
        config.ambient_chance = 0.0
    config.validate()


def _key_listener(controls: engine.Controls) -> None:
    """Single-key controls when we own a terminal: n = next, q = quit."""
    try:
        import termios
        import tty
    except ImportError:  # pragma: no cover - non-POSIX
        return
    fd = sys.stdin.fileno()
    try:
        saved = termios.tcgetattr(fd)
    except termios.error:  # pragma: no cover - not a tty
        return
    try:
        tty.setcbreak(fd)
        while not controls.stopping:
            char = sys.stdin.read(1)
            if not char:
                break
            if char in ("n", "s"):
                controls.skip()
            elif char == "q":
                controls.stop()
                break
    except (OSError, ValueError):  # pragma: no cover - stdin closed
        pass
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        except termios.error:  # pragma: no cover
            pass


def cmd_ui(args) -> int:
    from bgsoundtrack import webui

    if args.pick:
        return _pick_source(args)

    return webui.run(
        config_path=Path(args.config).expanduser() if args.config else None,
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
        root=str(Path(args.root).expanduser()) if args.root else None,
    )


def _pick_source(args) -> int:
    from bgsoundtrack import picker

    config = _load_config(args)
    result = picker.pick("folder", "Choose a music folder for bgst")
    if not result.available:
        print(f"no system file chooser here ({result.reason})", file=sys.stderr)
        print("use 'bgst source add PATH' instead", file=sys.stderr)
        return 1
    if not result.paths:
        print("nothing picked")
        return 0
    added = library.add_source(config, Path(result.paths[0]))
    config_module.save(config, Path(args.config).expanduser() if args.config else None)
    print(f"added music source {added}")
    return 0


def cmd_play(args) -> int:
    config, store, playlist = _load(args)
    _apply_play_overrides(config, args)
    try:
        backend = player.detect(args.player)
    except player.PlaybackError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    controls = engine.Controls()
    signal.signal(signal.SIGINT, lambda *_: controls.stop())
    signal.signal(signal.SIGTERM, lambda *_: controls.stop())

    rng = random.Random(args.seed) if args.seed is not None else random.Random()
    runner = engine.Engine(config, backend, rng=rng, controls=controls, store=store)

    def on_event(event: engine.Event) -> None:
        if event.kind == "track":
            print(f"♪ {event.path.name}", flush=True)
        elif event.kind == "ambient":
            print(f"  ~ {event.path.name} ({_fmt(event.duration)})", flush=True)
        elif event.kind == "silence":
            print(f"  . silence ({_fmt(event.duration)})", flush=True)
        elif event.kind == "done":
            print("stopped", flush=True)

    interactive = sys.stdin.isatty() and not args.no_keys
    if interactive:
        print(f"playing {playlist.name} with {backend.name} - n: next, q: quit")
        threading.Thread(target=_key_listener, args=(controls,), daemon=True).start()

    try:
        runner.run(on_event)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except player.PlaybackError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


# -- parser --------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bgst",
        description="Play a custom game soundtrack with ambient or silent gaps between songs.",
    )
    parser.add_argument("--version", action="version", version=f"bgst {__version__}")
    parser.add_argument("--root", help="custom soundtrack folder (overrides config)")
    parser.add_argument("--config", help="path to the config file")
    parser.add_argument(
        "--playlist", help="act on this playlist instead of the selected one"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create the custom soundtrack folder")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("link", help="symlink files or folders into the library")
    p.add_argument("paths", nargs="+")
    p.add_argument("--ambient", action="store_true", help="link into the ambient folder")
    p.add_argument("--no-recursive", action="store_true", help="do not descend into subfolders")
    p.add_argument("--relative", action="store_true", help="create relative symlinks")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("unlink", help="remove entries from the library")
    p.add_argument("names", nargs="+")
    p.add_argument("--ambient", action="store_true")
    p.set_defaults(func=cmd_unlink)

    p = sub.add_parser(
        "playlist", help="switch between sets of music, each with its own removals"
    )
    playlist_actions = p.add_subparsers(dest="action", required=True)
    sp = playlist_actions.add_parser("list", help="show every playlist")
    sp.set_defaults(func=cmd_playlist)
    sp = playlist_actions.add_parser("use", help="select a playlist")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_playlist)
    sp = playlist_actions.add_parser("new", help="create a playlist")
    sp.add_argument("name")
    sp.add_argument("--source", help="a folder to play in place straight away")
    sp.add_argument("--use", action="store_true", help="select it as well")
    sp.set_defaults(func=cmd_playlist)
    sp = playlist_actions.add_parser("rename", help="rename a playlist")
    sp.add_argument("name")
    sp.add_argument("new_name", metavar="NEW_NAME")
    sp.set_defaults(func=cmd_playlist)
    sp = playlist_actions.add_parser("remove", help="delete a playlist")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_playlist)
    sp = playlist_actions.add_parser("removed", help="tracks taken out of this playlist")
    sp.set_defaults(func=cmd_playlist)
    sp = playlist_actions.add_parser("restore", help="put removed tracks back")
    sp.add_argument("paths", nargs="*", metavar="PATH")
    sp.add_argument("--all", action="store_true")
    sp.set_defaults(func=cmd_playlist)

    p = sub.add_parser(
        "source",
        help="play whole folders in place (no symlinks, picked up as they change)",
    )
    source_actions = p.add_subparsers(dest="action", required=True)
    for action, blurb in (("add", "start playing a folder"), ("remove", "stop playing it")):
        sp = source_actions.add_parser(action, help=blurb)
        sp.add_argument("paths", nargs="+", metavar="PATH")
        sp.add_argument("--ambient", action="store_true", help="an ambience source")
        sp.set_defaults(func=cmd_source)
    sp = source_actions.add_parser("list", help="show the source folders")
    sp.set_defaults(func=cmd_source)

    p = sub.add_parser("list", help="show the library")
    p.add_argument("--music", action="store_true")
    p.add_argument("--ambient", action="store_true")
    p.add_argument("--targets", action="store_true", help="show what each link points at")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("prune", help="drop symlinks whose target is gone")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("config", help="show or change settings")
    p.add_argument("assignments", nargs="*", metavar="key=value")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("doctor", help="check the setup")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("ui", help="open the little control panel in a browser")
    p.add_argument(
        "--pick",
        action="store_true",
        help="open the system folder chooser to add a source, then exit",
    )
    p.add_argument("--port", type=int, default=8765)
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="0.0.0.0 to reach it from your phone on the same network",
    )
    p.add_argument("--no-browser", action="store_true", help="do not open a browser")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("play", help="start playing")
    p.add_argument("--gap-min", dest="gap_min", type=float, help="shortest gap, seconds")
    p.add_argument("--gap-max", dest="gap_max", type=float, help="longest gap, seconds")
    p.add_argument(
        "--ambient-chance",
        dest="ambient_chance",
        type=float,
        help="0..1 chance a gap gets ambience instead of silence",
    )
    p.add_argument("--volume", type=int, help="music volume, 0-100")
    p.add_argument("--ambient-volume", dest="ambient_volume", type=int, help="ambience volume, 0-100")
    p.add_argument("--no-shuffle", action="store_true")
    p.add_argument("--no-loop", action="store_true", help="stop after one pass")
    p.add_argument("--no-ambient", action="store_true", help="silent gaps only")
    p.add_argument("--no-keys", action="store_true", help="disable keyboard controls")
    p.add_argument("--player", help="force a backend (ffplay, mpv, afplay, vlc)")
    p.add_argument("--seed", type=int, help="deterministic shuffle and gaps")
    p.set_defaults(func=cmd_play)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (library.LibraryError, playlists.PlaylistError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # `bgst list | head` closes the pipe on us; exit quietly like `ls` does.
        try:
            sys.stdout.close()
        finally:
            os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
        return 0
