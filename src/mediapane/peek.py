"""`mediapane peek`: a small copy of an image or video for an AI agent to look at.

Agents pay for images by the pixel (Claude: roughly width x height / 750 tokens), and usually
only need to judge a plot's shape or a sample's quality. `peek` prints the path of a copy at
most `--max` px on its longest side (768 by default, about a quarter of a full-size figure's
cost); a video becomes a 2x2 sheet of frames from across the clip. Nothing else is printed, so
the call itself costs next to nothing. Copies are cached by content, so repeat peeks are free.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import warnings
from collections.abc import Generator
from pathlib import Path

from PIL import Image, ImageOps

from .formats import VIDEO_EXTS
from .ipc import cache_dir
from .media import FFMPEG_SAFE, FFMPEG_TIMEOUT

DEFAULT_MAX = 768
READABLE = {".png", ".jpg", ".jpeg", ".gif", ".webp"}  # what agents can read as is
PLAIN_MODES = {"RGB", "L", "1"}  # 8-bit, no transparency: fine to hand over unchanged
KEEP_DAYS = 7


def peek(path: Path, max_side: int = DEFAULT_MAX) -> Path:
    """Path of a copy of `path` no larger than `max_side` px (the file itself if it already is
    small, plain 8-bit and in a format agents read)."""
    st = path.stat()
    key = hashlib.sha256(f"{path.resolve()}:{st.st_mtime_ns}:{st.st_size}:{max_side}".encode())
    out = _dir() / f"{path.stem[:40]}-{key.hexdigest()[:12]}.png"
    if out.exists():
        return out
    if path.suffix.lower() in VIDEO_EXTS:
        image = _contact_sheet(path, max_side)
    else:
        with warnings.catch_warnings(), _no_size_limit():
            # A file the user or their agent picked, not an upload: big scans and renders are
            # exactly what peek is for, so no decompression-bomb limit (or warning on stderr)
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(path) as im:
                rotated = im.getexif().get(0x0112, 1) not in (0, 1)  # phone photos
                plain = im.mode in PLAIN_MODES and "transparency" not in im.info
                small = max(im.size) <= max_side and getattr(im, "n_frames", 1) == 1
                if small and plain and not rotated and path.suffix.lower() in READABLE:
                    return path
                image = _viewable(ImageOps.exif_transpose(im) if rotated else im)
                image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    tmp = out.with_suffix(f".{os.getpid()}.tmp")
    image.save(tmp, format="PNG", optimize=True)
    os.replace(tmp, out)  # a concurrent or interrupted peek never serves half a file
    return out


def _viewable(im: Image.Image) -> Image.Image:
    """8-bit RGB as a viewer would show it: high-bit-depth and float images stretched to their
    own range, transparency (alpha or a palette's) flattened onto white."""
    if im.mode in ("I;16", "I;16B", "I;16L", "I", "F"):
        lo, hi = im.getextrema()
        scale = 255 / (hi - lo) if hi > lo else 0
        return im.point(lambda v: (v - lo) * scale).convert("L").convert("RGB")
    if "A" in im.getbands() or "transparency" in im.info:
        rgba = im.convert("RGBA")
        white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(white, rgba).convert("RGB")
    return im.convert("RGB")


@contextlib.contextmanager
def _no_size_limit() -> Generator[None, None, None]:
    old = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = None
    try:
        yield
    finally:
        Image.MAX_IMAGE_PIXELS = old


def _contact_sheet(path: Path, max_side: int) -> Image.Image:
    """Four frames from across the video, 2x2, at most `max_side` px."""
    ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")
    if not (ffprobe and ffmpeg):
        raise RuntimeError("needs ffmpeg for video")
    meta = subprocess.run(
        [ffprobe, "-v", "error", *FFMPEG_SAFE, "-show_entries", "format=duration", "-of", "json",
         str(path)],
        capture_output=True, text=True, timeout=FFMPEG_TIMEOUT, check=False,
    )  # fmt: skip
    try:
        duration = float(json.loads(meta.stdout)["format"]["duration"])
    except (KeyError, ValueError, TypeError) as e:
        raise RuntimeError("not a video ffmpeg can read") from e
    frames = []
    with tempfile.TemporaryDirectory(prefix="mediapane-peek-") as tmp:
        for i, at in enumerate((0.1, 0.35, 0.6, 0.85)):
            shot = Path(tmp) / f"{i}.png"
            subprocess.run(
                [ffmpeg, "-v", "error", "-nostdin", "-y", *FFMPEG_SAFE, "-ss",
                 f"{duration * at:.2f}", "-i", str(path), "-frames:v", "1", str(shot)],
                capture_output=True, timeout=FFMPEG_TIMEOUT, check=False,
            )  # fmt: skip
            if shot.exists():
                with Image.open(shot) as im:
                    frames.append(im.convert("RGB"))
    if not frames:
        raise RuntimeError("couldn't read any frames")
    cell = (max_side // 2, max_side // 2)
    for f in frames:
        f.thumbnail(cell, Image.Resampling.LANCZOS)
    w, h = max(f.width for f in frames), max(f.height for f in frames)
    sheet = Image.new("RGB", (2 * w + 2, 2 * h + 2), (0, 0, 0))
    for i, f in enumerate(frames):
        sheet.paste(f, ((i % 2) * (w + 2), (i // 2) * (h + 2)))
    return sheet


def _dir() -> Path:
    d = cache_dir() / "peek"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    cutoff = time.time() - KEEP_DAYS * 86400
    for old in d.glob("*.png"):  # small files, but don't let them pile up forever
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
    return d
