"""Compare two images with Pillow alone and push the views to the pane as one series."""

from __future__ import annotations

import functools
import io
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mediapane import ipc
from mediapane.ipc import NO_PANE
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps, ImageStat

BG = (24, 24, 28)
FG = (230, 230, 230)
GAP = 8


class Unreadable(Exception):
    pass


@dataclass(frozen=True)
class Stats:
    changed: int  # pixels where any channel differs
    total: int
    max: int  # 0-255, over channels
    mean: float  # per pixel, of the max over channels

    @property
    def identical(self) -> bool:
        return self.changed == 0

    @property
    def percent(self) -> str:
        p = 100 * self.changed / self.total
        if self.changed and p < 0.1:
            return "<0.1%"
        if self.changed < self.total and p > 99.9:
            return ">99.9%"
        return f"{p:.3g}%"


def run(a: str, b: str, *, name: str | None = None, mode: str = "all") -> int:
    try:
        ra, rb = _read(a), _read(b)
        if ra is None and rb is None:
            raise Unreadable(f"neither {a} nor {b} exists")
        ia = _load(ra, a) if ra is not None else None
        ib = _load(rb, b) if rb is not None else None
    except Unreadable as e:
        print(f"mediapane diff: {e}", file=sys.stderr)
        return 2

    label = name or " vs ".join(Path(p).name for p, r in ((a, ra), (b, rb)) if r is not None)
    stem = Path(name).stem if name else label.replace(" ", "-")
    tag = f"diff: {label}"

    if ia is None or ib is None:  # an added or deleted file (git passes /dev/null)
        which, data = ("B", rb) if ia is None else ("A", ra)
        print(f"{label}: only {which} exists ({'A' if which == 'B' else 'B'} is missing)")
        fname = Path(name or (b if which == "B" else a)).name
        return _send([(data, fname, f"{which} only")], tag)

    if ia.size != ib.size:
        print(f"note: B is {ib.width}x{ib.height}, scaled to A's {ia.width}x{ia.height}")
        ib = ib.resize(ia.size, Image.Resampling.LANCZOS)

    stats, heat = heatmap(ia, ib)
    if stats.identical:
        print(f"{label}: identical ({ia.width}x{ia.height})")
        return _send([(_png(side_by_side(ia, ib)), f"{stem}-side.png", "side by side")], tag)
    print(f"{label}: {stats.percent} of pixels differ (max {stats.max}/255, mean {stats.mean:.2f})")

    items = []
    if mode in ("all", "side"):
        items.append((_png(side_by_side(ia, ib)), f"{stem}-side.png", "side by side"))
    if mode in ("all", "flicker"):
        items.append((flicker(ia, ib), f"{stem}-flicker.png", "flicker"))
    if mode in ("all", "heat"):
        items.append((_png(heat), f"{stem}-diff.png", f"difference ({stats.percent} changed)"))
    return _send(items, tag)


def _read(path: str) -> bytes | None:
    """The file's bytes, or None if it doesn't exist (git difftool passes /dev/null for the
    missing side of an added or deleted file). Read whole, so pipes (`<(git show ...)`) work."""
    p = Path(path)
    try:
        if not p.exists() or os.path.samefile(p, os.devnull):
            return None
        if p.is_file() and p.stat().st_size == 0:  # an empty placeholder, as /dev/null
            return None
        return p.read_bytes()
    except OSError as e:
        raise Unreadable(f"can't read {path}: {e.strerror}") from e


def _load(data: bytes, path: str) -> Image.Image:
    """First frame, upright, as RGBA, with fully transparent pixels zeroed so colour hidden
    under alpha 0 doesn't count as a difference."""
    try:
        im = Image.open(io.BytesIO(data))
        im.seek(0)
        im = ImageOps.exif_transpose(im)
        if im.mode in ("I", "F") or im.mode.startswith("I;16"):
            im = im.convert("I")
            if im.getextrema()[1] > 255:  # 16-bit: keep the top 8 bits
                im = im.point(lambda v: v * (1 / 257))
            im = im.convert("L")
        im = im.convert("RGBA")
    except Exception as e:  # Pillow raises many types for bad or truncated files
        raise Unreadable(f"can't read {path} as an image: {e}") from e
    alpha = im.getchannel("A")
    if alpha.getextrema()[0] == 0:
        im = Image.composite(im, Image.new("RGBA", im.size), alpha.point(lambda v: 255 * (v > 0)))
    return im


def heatmap(a: Image.Image, b: Image.Image) -> tuple[Stats, Image.Image]:
    """Per-pixel max-over-channels |A - B|, drawn in heat colours over a dim copy of A, with
    every nonzero difference made visible: the scale is stretched to the largest difference and
    starts at plain red."""
    d = functools.reduce(ImageChops.lighter, ImageChops.difference(a, b).split())  # (L)
    hist = d.histogram()
    total = a.width * a.height
    stats = Stats(total - hist[0], total, d.getextrema()[1], ImageStat.Stat(d).mean[0])
    hi = max(stats.max, 1)
    amp = d.point(lambda v: 0 if v == 0 else 128 + round(127 * v / hi))
    heat = ImageOps.colorize(amp, black=(60, 0, 0), mid=(230, 50, 20), white=(255, 245, 160))
    base = _flat(a).convert("L").point(lambda v: v // 5).convert("RGB")
    out = Image.composite(heat, base, d.point(lambda v: 255 * (v > 0)))
    text = f"max {stats.max}/255   mean {stats.mean:.2f}   {stats.percent} of pixels differ"
    return stats, _labelled(out, text)


def side_by_side(a: Image.Image, b: Image.Image) -> Image.Image:
    font = _font()
    top = _label_height(font)
    out = Image.new("RGB", (a.width + GAP + b.width, max(a.height, b.height) + top), BG)
    out.paste(_flat(a), (0, top))
    out.paste(_flat(b), (a.width + GAP, top))
    draw = ImageDraw.Draw(out)
    draw.text((2, 2), "A", fill=FG, font=font)
    draw.text((a.width + GAP + 2, 2), "B", fill=FG, font=font)
    return out


def flicker(a: Image.Image, b: Image.Image, fps: float = 2.0) -> bytes:
    """An animated PNG alternating A and B. The labels also keep the frames distinct, which
    matters: Pillow merges identical consecutive APNG frames."""
    frames = [_labelled(_flat(a), "A"), _labelled(_flat(b), "B")]
    buf = io.BytesIO()
    frames[0].save(
        buf,
        format="PNG",
        save_all=True,
        append_images=frames[1:],
        duration=round(1000 / fps),
        loop=0,
        compress_level=3,
    )
    return buf.getvalue()


def _flat(im: Image.Image) -> Image.Image:
    out = Image.new("RGB", im.size, BG)
    out.paste(im, mask=im.getchannel("A"))
    return out


def _labelled(im: Image.Image, text: str) -> Image.Image:
    font = _font()
    top = _label_height(font)
    out = Image.new("RGB", (im.width, im.height + top), BG)
    out.paste(im, (0, top))
    ImageDraw.Draw(out).text((2, 2), text, fill=FG, font=font)
    return out


@functools.cache
def _font() -> Any:
    return ImageFont.load_default()


def _label_height(font: Any) -> int:
    return int(font.getbbox("AB")[3]) + 4


def _png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="PNG", compress_level=3)
    return buf.getvalue()


def _send(items: list[tuple[bytes, str, str]], tag: str) -> int:
    spooled = False
    for data, fname, step in items:
        reply = ipc.request("push", data, name=fname, tag=tag, step=step)
        if reply is None:
            print(NO_PANE, file=sys.stderr)
            return 1
        if not reply.get("ok"):
            print(f"mediapane diff: {fname}: {reply.get('error', 'rejected')}", file=sys.stderr)
            return 1
        spooled = spooled or bool(reply.get("spooled"))
    if spooled:
        print("mediapane: no pane reachable; spooled for a relay", file=sys.stderr)
    return 0
