"""`mediapane watch`: push media files as they appear in (or change in) directories."""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .formats import MEDIA_EXTS

State = tuple[float, int]  # (mtime, size)


def series_key(path: Path, root: Path | None = None) -> tuple[str, str] | None:
    """(tag, step) for files numbered by step: `samples_001000.png` -> ("samples_#.png",
    "1000"). The last number in the name is the step; None if there is no number. Under a
    watched `root`, the tag includes the folder, so `run_a/` and `run_b/` stay apart."""
    m = re.search(r"(\d+)(?!.*\d)", path.stem)
    if not m:
        return None
    tag = path.stem[: m.start()] + "#" + path.stem[m.end() :] + path.suffix
    if root is not None and path.parent != root:
        try:
            tag = f"{path.parent.relative_to(root).as_posix()}/{tag}"
        except ValueError:
            pass
    return tag, str(int(m[1]))


def scan(dirs: list[Path], recursive: bool) -> dict[Path, State]:
    found = {}
    for d in dirs:
        try:
            entries = list(d.rglob("*") if recursive else d.iterdir())
        except OSError:  # directory removed or unreadable mid-scan: try again next poll
            continue
        for p in entries:
            if p.suffix.lower() in MEDIA_EXTS and not p.name.startswith("."):
                try:
                    st = p.stat()
                except OSError:
                    continue
                found[p] = (st.st_mtime, st.st_size)
    return found


class Watcher:
    """Reports a file once it's new or changed and its size and mtime have held still for one
    poll, so half-written files (a plot mid-savefig, a video mid-encode) aren't pushed."""

    def __init__(self, dirs: list[Path], recursive: bool = False, existing: bool = False) -> None:
        self.dirs, self.recursive = dirs, recursive
        self.seen: dict[Path, State] = {} if existing else scan(dirs, recursive)
        self.pending: dict[Path, State] = {}

    def poll(self) -> list[Path]:
        ready = []
        for p, state in sorted(scan(self.dirs, self.recursive).items(), key=lambda kv: kv[1][0]):
            if self.seen.get(p) == state:
                continue
            if self.pending.get(p) == state:
                ready.append(p)
                self.seen[p] = state
                del self.pending[p]
            else:
                self.pending[p] = state
        return ready


def watch(
    dirs: list[Path],
    push: Callable[[Path], dict | None],
    *,
    interval: float = 1.0,
    recursive: bool = False,
    existing: bool = False,
    quiet: bool = False,
) -> None:
    w = Watcher(dirs, recursive, existing)
    while True:
        for p in w.poll():
            reply = push(p)
            if reply is None:
                print(f"no pane for {p}", file=sys.stderr, flush=True)
            elif not reply.get("ok"):
                print(f"skipped {p}: {reply.get('error', 'rejected')}", file=sys.stderr, flush=True)
            elif not quiet:
                print(f"pushed {p}", flush=True)
        time.sleep(interval)
