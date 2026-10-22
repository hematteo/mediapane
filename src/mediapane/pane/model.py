"""The pane's data: items (single media or a series of steps) and connections."""

from __future__ import annotations

import hashlib
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from .. import plugins
from ..media import Media
from .video import VideoPlayer


@dataclass
class Step:
    """One item of a series."""

    media: Media
    workdir: Path
    label: str
    host: str
    added: float


@dataclass
class Item:
    id: int  # kitty image id
    name: str  # sanitised
    host: str  # sanitised
    media: Media
    workdir: Path
    added: float
    cells: tuple[int, int] = (0, 0)  # gallery size in cells
    frame: int = 0
    next_frame: float = 0.0
    playing: bool = True  # animations loop and videos play while visible
    video: VideoPlayer | None = None
    position: float = 0.0  # seconds of video played
    last_png: bytes | None = None  # most recent video frame, kept only while on screen
    waiting: bool = False  # animation stalled on a frame being scaled (over SSH)
    broken: str = ""  # why it can't be displayed, if it can't
    digest: str = ""  # content hash, to spot the same file pushed twice
    tag: str = ""  # a series: one gallery entry for every push with this tag
    steps: list[Step] = field(default_factory=list)  # its items; `media` is steps[cur].media
    cur: int = 0
    lapse: bool = False  # time-lapse playing through the steps
    next_lapse: float = 0.0
    native: bool = False  # animation stored in and played by the terminal itself
    native_failed: bool = False  # too big to store there: stream its frames instead

    def info(self) -> plugins.ItemInfo:
        step = self.steps[self.cur].label if self.steps else ""
        m = self.media
        return plugins.ItemInfo(
            self.name, self.host, m.kind, m.path, m.still, m.width, m.height, self.tag, step
        )


@dataclass
class Conn:
    sock: socket.socket
    buf: bytearray = field(default_factory=bytearray)
    header: dict | None = None
    file: BinaryIO | None = None
    path: Path | None = None
    remaining: int = 0
    seq: int = 0
    digest: Any = field(default_factory=hashlib.sha256)  # of the payload, as it streams in
