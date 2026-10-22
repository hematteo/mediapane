"""Terminal facts: size in cells and pixels, tmux/SSH detection, raw input, graphics support."""

from __future__ import annotations

import contextlib
import fcntl
import os
import select
import struct
import termios
import time
import tty
from collections.abc import Generator

from .kitty import tmux_wrap

FALLBACK_CELL = (10.0, 20.0)  # cell px when the terminal doesn't report pixels: assume 1:2


def winsize(fd: int) -> tuple[int, int, int, int]:
    """(rows, cols, width px, height px); pixels are 0 where the terminal doesn't report them."""
    rows, cols, xpix, ypix = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
    return rows, cols, xpix, ypix


def cell_size(fd: int) -> tuple[float, float]:
    rows, cols, xpix, ypix = winsize(fd)
    if rows and cols and xpix and ypix:
        return xpix / cols, ypix / rows
    return FALLBACK_CELL


def in_tmux() -> bool:
    return bool(os.environ.get("TMUX"))


def over_ssh() -> bool:
    return bool(os.environ.get("SSH_TTY") or os.environ.get("SSH_CONNECTION"))


def direct_transfer() -> bool:
    """Whether image data must travel inline: the terminal can only read our files by path when
    it runs on this machine. MEDIAPANE_TRANSFER=file|direct overrides the guess."""
    mode = os.environ.get("MEDIAPANE_TRANSFER", "").lower()
    if mode in ("file", "direct"):
        return mode == "direct"
    return over_ssh()


@contextlib.contextmanager
def cbreak(fd: int) -> Generator[None, None, None]:
    """Keys arrive one at a time, unechoed; Ctrl-C still raises KeyboardInterrupt."""
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd, termios.TCSANOW)  # not the default TCSAFLUSH: keep keys typed early
        yield
    finally:
        with contextlib.suppress(termios.error):  # the terminal may be gone already (closed split)
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def query_graphics(fd_in: int, fd_out: int, timeout: float = 1.0) -> bool | None:
    """Ask the terminal whether it speaks the kitty graphics protocol. Sends a tiny query image
    followed by a device-attributes request, which every terminal answers, so a missing graphics
    reply can be told apart from a slow one. None if the terminal answered nothing at all."""
    query = "\x1b_Gi=31,s=1,v=1,a=q,t=d,f=24;AAAA\x1b\\"
    if in_tmux():
        query = tmux_wrap(query)
    reply = b""
    with cbreak(fd_in):
        os.write(fd_out, (query + "\x1b[c").encode())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd_in], [], [], max(0.0, deadline - time.monotonic()))
            if not ready:
                break
            reply += os.read(fd_in, 1024)
            if b"\x1b[?" in reply and reply.rstrip().endswith(b"c"):
                break
    if not reply:
        return None
    return b"_Gi=31;OK" in reply
