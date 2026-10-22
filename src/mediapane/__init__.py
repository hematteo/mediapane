"""mediapane: a media sidecar for your terminal.

    import mediapane
    mediapane.show("plot.png")           # a file
    mediapane.show(fig)                  # a matplotlib figure
    mediapane.show(images)               # (N, C, H, W) / (N, H, W, C) batch -> grid
    mediapane.show(frames, fps=12)       # (T, H, W, C) -> animation

With no pane running, images are drawn inline at the cursor instead (in terminals that support
the kitty graphics protocol).
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from typing import Any

from . import ipc

__all__ = ["show", "push"]
__version__ = "0.2.0"


def show(
    obj: Any,
    name: str | None = None,
    *,
    fps: float | None = None,
    video: bool = False,
    tag: str | None = None,
    step: int | float | str | None = None,
) -> bool:
    """Send `obj` to the pane (see module docstring for what it accepts). With `tag`, it joins a
    series: one gallery entry per tag, stepped through with ←/→ and played as a time-lapse with
    `t`; `step` labels it (e.g. the training step). Returns True if a pane took it; otherwise
    draws it inline when stdout is a capable terminal, and returns False."""
    from . import convert  # imports Pillow: kept off the path of `mp` and `mediapane push`

    data, fname = convert.to_media(obj, name, fps=fps, video=video)
    meta = {k: str(v) for k, v in (("tag", tag), ("step", step)) if v is not None}
    if ipc.push(data, fname, **meta):
        return True
    _inline(data)
    return False


def push(
    path: str | os.PathLike[str],
    name: str | None = None,
    *,
    tag: str | None = None,
    step: int | float | str | None = None,
) -> bool:
    """Send a file to the pane (streamed, so any size is fine); False if no pane took it."""
    meta = {k: str(v) for k, v in (("tag", tag), ("step", step)) if v is not None}
    reply = ipc.push_file(Path(path), name, **meta)
    return bool(reply and reply.get("ok"))


def _inline(data: bytes) -> None:
    """Draw the (first frame of the) image at the cursor, sized to the terminal."""
    from PIL import Image, UnidentifiedImageError

    from . import term
    from .kitty import Graphics
    from .layout import fit

    if not sys.stdout.isatty():
        print("mediapane: no pane running (start one with `mediapane open`)", file=sys.stderr)
        return
    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
            buf = io.BytesIO()
            im.convert("RGBA").save(buf, format="PNG")
    except (UnidentifiedImageError, OSError):
        print("mediapane: no pane running, and this can't be drawn inline", file=sys.stderr)
        return
    fd = sys.stdout.fileno()
    rows, cols, _, _ = term.winsize(fd)
    cw, ch = term.cell_size(fd)
    c, _ = fit(w, h, cols - 1, max(4, rows - 2), cw, ch, upscale=1.0)
    gfx = Graphics(direct=True, tmux=term.in_tmux())
    sys.stdout.flush()
    os.write(fd, (gfx.inline(buf.getvalue(), c) + "\n").encode())
