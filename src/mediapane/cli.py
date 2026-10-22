"""Command line: `mediapane` (alias `mp`)."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__, ipc

NO_PANE = ipc.NO_PANE
CORE = {"pane", "open", "push", "watch", "ssh", "doctor", "clear", "plugins", "peek"}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    first = argv[0] if argv else ""
    if first == "ssh":  # everything after goes to ssh untouched
        from .remote import ssh

        return ssh(argv[1:])
    # Plugins load only for a word that isn't a core command or a media file (so never for
    # `mp plot.png`). A plugin command beats a same-named file or folder without a media
    # extension: Claude's hook runs `mediapane claude hook` in whatever project it's in.
    if first and first not in CORE and not first.startswith("-") and not _is_media(first):
        from . import plugins

        api = plugins.load()
        if command := api.commands.get(first):
            return _run_plugin(command, argv[1:])
        if api.errors and not Path(first).exists():
            names = ", ".join(n for n, _ in api.errors)
            print(f"mediapane: plugin failed to load: {names} (see `mediapane plugins`)",
                  file=sys.stderr)  # fmt: skip
    push_option = first.split("=")[0] in ("--tag", "--step", "--name")
    if first and first not in CORE and (first == "-" or not first.startswith("-") or push_option):
        argv = ["push", *argv]  # `mp plot.png`, `mp --tag t x.png`, `cat x.png | mp -`
    if not argv and not sys.stdin.isatty():
        argv = ["push", "-"]  # `cat x.png | mp`

    p = argparse.ArgumentParser(
        prog="mediapane",
        description="A media sidecar for your terminal: keep a pane open and push images, GIFs "
        "and videos to it from anywhere.",
        epilog=_plugin_epilog() if first in ("-h", "--help") else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"mediapane {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="command")
    sp = sub.add_parser("pane", help="run the pane in this terminal (the default)")
    sp.add_argument("--socket", type=Path, help="listen here instead of the default socket")
    sp.add_argument("--resume", action="store_true", help="bring back the last session's items")
    so = sub.add_parser("open", help="open a pane in a new split (tmux, Ghostty, kitty, WezTerm)")
    so.add_argument("-d", "--down", action="store_true", help="split downwards")
    su = sub.add_parser("push", help="send files to the pane (- reads stdin)")
    su.add_argument("files", nargs="+")
    su.add_argument("--name", help="display name (default: the file name)")
    su.add_argument("--tag", help="add to a series: one gallery entry, stepped with ←/→")
    su.add_argument("--step", help="this item's step in the series (e.g. a training step)")
    sw = sub.add_parser("watch", help="push media files as they appear in directories")
    sw.add_argument("dirs", nargs="+", type=Path)
    sw.add_argument("-r", "--recursive", action="store_true")
    sw.add_argument("--existing", action="store_true", help="also push files already there")
    sw.add_argument("--interval", type=float, default=1.0, help="seconds between scans")
    sw.add_argument("-q", "--quiet", action="store_true", help="print only errors")
    sw.add_argument(
        "--series",
        action="store_true",
        help="group files whose names differ only in a number (samples_100.png, samples_200.png)"
        " into a series, stepped by that number",
    )
    sub.add_parser("ssh", help="ssh with a socket forwarded to this pane (args go to ssh)")
    sub.add_parser("doctor", help="check what works in this terminal, with fixes")
    sub.add_parser("clear", help="remove everything from the pane")
    sub.add_parser("plugins", help="list installed plugins and ones you can add")
    sk = sub.add_parser(
        "peek",
        help="print the path of a small copy of an image or video, for an AI agent to look at "
        "(costs it far fewer tokens than the original)",
    )
    sk.add_argument("file", type=Path)
    sk.add_argument("--max", type=int, default=768, help="longest side in px (default 768)")
    args = p.parse_args(argv)

    if args.cmd in (None, "pane"):
        return _pane(getattr(args, "socket", None), getattr(args, "resume", False))
    if args.cmd == "open":
        return _open(args.down)
    if args.cmd == "push":
        return _push(args.files, args.name, args.tag, args.step)
    if args.cmd == "watch":
        return _watch(args)
    if args.cmd == "doctor":
        from .doctor import doctor

        return doctor()
    if args.cmd == "plugins":
        from .plugins import report

        return report()
    if args.cmd == "peek":
        return _peek(args.file, args.max)
    if args.cmd == "clear":
        if ipc.request("clear") is None:
            print(NO_PANE, file=sys.stderr)
            return 1
        return 0
    return 2


def _is_media(word: str) -> bool:
    from .formats import MEDIA_EXTS

    return Path(word).suffix.lower() in MEDIA_EXTS and Path(word).is_file()


def _plugin_epilog() -> str | None:
    from . import plugins

    commands = plugins.load().commands
    if not commands:
        return "plugins add commands: see `mediapane plugins`"
    width = max(len(n) for n in commands) + 2
    lines = [f"  {c.name:<{width}}{c.help}  [{c.plugin}]" for c in commands.values()]
    return "plugin commands:\n" + "\n".join(lines)


def _run_plugin(command, argv: list[str]) -> int:  # noqa: ANN001 - plugins.Command
    p = argparse.ArgumentParser(prog=f"mediapane {command.name}", description=command.help)
    if command.setup:
        command.setup(p)
    try:
        return int(command.run(p.parse_args(argv)) or 0)
    except KeyboardInterrupt:
        return 130


def _pane(sock: Path | None, resume: bool = False) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(
            "mediapane: the pane needs a terminal; to send media, use `mediapane push`",
            file=sys.stderr,
        )
        return 2
    from .pane import Pane

    Pane(sock or ipc.default_socket(), resume=resume).run()
    # Don't wait for a decode still running on a worker thread (a long GIF): its result is moot
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


def _open(down: bool) -> int:
    from .split import open_split

    if reply := ipc.request("ping", path=ipc.default_socket(), timeout=1.0):
        print(f"mediapane: a pane is already running (pid {reply.get('pid')})")
        return 0
    try:
        where = open_split(down)
    except Exception as e:
        print(f"mediapane: {e}", file=sys.stderr)
        return 1
    print(f"mediapane: opened a pane in {where}")
    return 0


def _push(files: list[str], name: str | None, tag: str | None, step: str | None) -> int:
    from .media import sniff_name

    meta = {k: v for k, v in (("tag", tag), ("step", step)) if v is not None}
    for f in files:
        if f == "-":
            data = sys.stdin.buffer.read()
            reply = ipc.request("push", data, name=name or sniff_name(data), **meta)
        elif Path(f).is_file():
            reply = ipc.push_file(Path(f), name, **meta)
        else:
            print(f"mediapane: no such file: {f}", file=sys.stderr)
            return 1
        if not _report(reply, f):
            return 1
    return 0


def _peek(path: Path, max_side: int) -> int:
    from .peek import peek

    try:
        print(peek(path, max(16, max_side)))
    except Exception as e:  # one line, whatever went wrong: this output goes to an agent
        print(f"mediapane: can't peek at {path}: {e}", file=sys.stderr)
        return 1
    return 0


def _report(reply: dict | None, what: str) -> bool:
    if reply is None:
        print(NO_PANE, file=sys.stderr)
        return False
    if reply.get("spooled"):
        print(f"mediapane: no pane reachable; spooled {what} for a relay", file=sys.stderr)
        return True
    if not reply.get("ok"):
        print(f"mediapane: {what}: {reply.get('error', 'rejected')}", file=sys.stderr)
        return False
    return True


def _watch(args: argparse.Namespace) -> int:
    from .watch import series_key, watch

    for d in args.dirs:
        if not d.is_dir():
            print(f"mediapane: not a directory: {d}", file=sys.stderr)
            return 1

    def push(p: Path) -> dict | None:
        meta = {}
        root = next((d for d in args.dirs if d in p.parents), None)
        if args.series and (key := series_key(p, root)):
            meta = {"tag": key[0], "step": key[1]}
        return ipc.push_file(p, **meta)

    if not args.quiet:
        print(f"watching {', '.join(map(str, args.dirs))} (Ctrl-C to stop)", flush=True)
    try:
        watch(
            args.dirs,
            push,
            interval=args.interval,
            recursive=args.recursive,
            existing=args.existing,
            quiet=args.quiet,
        )
    except KeyboardInterrupt:
        pass
    return 0
