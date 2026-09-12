"""Command line interface: bgst <command>."""

from __future__ import annotations

import argparse
import random
import signal
import sys
import threading
from pathlib import Path

from bgsoundtrack import __version__, config as config_module, engine, library, player
from bgsoundtrack.config import Config


def _fmt(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}:{secs:02d}" if minutes else f"{secs}s"


def _load_config(args) -> Config:
    config = config_module.load(Path(args.config).expanduser() if args.config else None)
    if args.root:
        config.root = str(Path(args.root).expanduser())
    config.validate()
    return config


# -- commands ------------------------------------------------------------


def cmd_init(args) -> int:
    config = _load_config(args)
    created = library.init(config)
    print(f"custom soundtrack folder: {config.root_path}")
    for path in created:
        print(f"  created {path}")
    if not created:
        print("  already set up")
    print("\nAdd music with:   bgst link ~/Music/some-album")
    print("Add ambience with: bgst link --ambient ~/Sounds/rain.ogg")
    return 0


def cmd_link(args) -> int:
    config = _load_config(args)
    library.init(config)
    section = "ambient" if args.ambient else "music"
    result = library.link(
        config,
        [Path(p) for p in args.paths],
        section=section,
        recursive=not args.no_recursive,
        relative=args.relative,
    )
    for path in result.linked:
        print(f"linked {path.name}")
    for path, reason in result.skipped:
        print(f"skipped {path.name}: {reason}", file=sys.stderr)
    print(f"\n{len(result.linked)} linked into {section}, {len(result.skipped)} skipped")
    return 0


def cmd_unlink(args) -> int:
    config = _load_config(args)
    section = "ambient" if args.ambient else "music"
    for path in library.unlink(config, args.names, section=section):
        print(f"removed {path.name}")
    return 0


def cmd_list(args) -> int:
    config = _load_config(args)
    sections = ("ambient",) if args.ambient else ("music",) if args.music else library.SECTIONS
    for section in sections:
        entries = library.tracks(config, section)
        print(f"{section} ({len(entries)}):")
        for entry in entries:
            if args.targets and entry.is_symlink():
                print(f"  {entry.name} -> {entry.resolve()}")
            else:
                print(f"  {entry.name}")
        for entry in library.broken(config, section):
            print(f"  {entry.name} -> BROKEN", file=sys.stderr)
        print()
    return 0


def cmd_prune(args) -> int:
    config = _load_config(args)
    removed = library.prune(config)
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
    config = _load_config(args)
    print(f"version         {__version__}")
    print(f"root            {config.root_path}")
    print(f"root exists     {config.root_path.is_dir()}")
    for section in library.SECTIONS:
        entries = library.tracks(config, section)
        bad = library.broken(config, section)
        print(f"{section:15} {len(entries)} playable, {len(bad)} broken link(s)")
    found = player.available()
    print(f"players found   {', '.join(found) if found else 'NONE'}")
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


def cmd_play(args) -> int:
    config = _load_config(args)
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
    runner = engine.Engine(config, backend, rng=rng, controls=controls)

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
        print(f"playing with {backend.name} - n: next, q: quit")
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
    except (library.LibraryError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
