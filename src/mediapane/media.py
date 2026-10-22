"""What the pane does with a received file: work out its kind and prepare PNGs for display.

Terminals only take PNG (or raw pixels), so every image gets a PNG still; animations (GIF, APNG,
animated WebP) are split into PNG frames once, up front; videos get a poster frame and are decoded
by ffmpeg only while they play.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageOps, ImageSequence, UnidentifiedImageError

from .formats import IMAGE_EXTS, VIDEO_EXTS  # noqa: F401 - re-exported

MAX_FRAME_WIDTH = 960  # animation frames are stored at most this wide
MAX_FRAMES = 2000
FFMPEG_TIMEOUT = 60  # seconds for ffprobe / a poster frame
# Pushes are untrusted: no playlists or concat lists that could make ffmpeg open other files
# or URLs, just these containers, read from the one local file
FFMPEG_SAFE = ["-protocol_whitelist", "file,pipe",
               "-format_whitelist", "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi"]  # fmt: skip


@dataclass
class Media:
    path: Path
    kind: str  # "image" | "anim" | "video"
    width: int
    height: int
    still: Path | None  # PNG shown before / without playback
    frames: list[tuple[Path, float]] = field(default_factory=list)  # anim: (PNG, seconds)
    duration: float | None = None

    @property
    def label(self) -> str:
        if self.kind == "anim":
            return f"▶ {len(self.frames)} frames, {self.duration:.1f}s"
        if self.kind == "video":
            if self.duration is None:
                return "▶ video"
            m, s = divmod(int(self.duration + 0.5), 60)
            return f"▶ {m}:{s:02d}"
        return ""


def probe(path: Path, workdir: Path) -> Media:
    """Inspect `path` and write whatever PNGs display needs into `workdir`. Raises ValueError
    with a short reason for anything that can't be shown."""
    workdir.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() not in VIDEO_EXTS:
        try:
            return _probe_image(path, workdir)
        except UnidentifiedImageError:
            pass  # not an image: maybe a video without a telling extension
        except (OSError, SyntaxError, ValueError) as e:  # Pillow knows the format but can't decode
            raise ValueError(f"damaged image ({e})") from e
    return _probe_video(path, workdir)


def _probe_image(path: Path, workdir: Path) -> Media:
    with Image.open(path) as im:
        if getattr(im, "n_frames", 1) > 1:
            return _probe_animation(path, im, workdir)
        # Phone photos store rotation in EXIF (tag 0x0112); transpose only when there is one
        rotated = im.getexif().get(0x0112, 1) not in (0, 1)
        oriented = ImageOps.exif_transpose(im) if rotated else im
        oriented.load()  # decode fully now: a truncated file fails here, not mid-render
        w, h = oriented.size
        if im.format == "PNG" and oriented is im:
            still = path
        else:
            still = workdir / "still.png"
            has_alpha = "A" in oriented.getbands() or "transparency" in oriented.info
            oriented.convert("RGBA" if has_alpha else "RGB").save(still, compress_level=1)
        return Media(path, "image", w, h, still)


def _probe_animation(path: Path, im: Image.Image, workdir: Path) -> Media:
    w, h = im.size
    frames = []
    total = 0.0
    for i, frame in enumerate(ImageSequence.Iterator(im)):
        if i >= MAX_FRAMES:
            break
        delay = (frame.info.get("duration") or 100) / 1000
        rgba = frame.convert("RGBA")
        if rgba.width > MAX_FRAME_WIDTH:
            rgba = rgba.resize(
                (MAX_FRAME_WIDTH, round(rgba.height * MAX_FRAME_WIDTH / rgba.width)),
                Image.Resampling.LANCZOS,
            )
        out = workdir / f"frame{i:05d}.png"
        rgba.save(out, compress_level=1)
        frames.append((out, max(0.02, delay)))
        total += delay
    return Media(path, "anim", w, h, frames[0][0], frames, total)


def _probe_video(path: Path, workdir: Path) -> Media:
    ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")
    if not (ffprobe and ffmpeg):
        if path.suffix.lower() in VIDEO_EXTS:
            return Media(path, "video", 0, 0, None)  # shown as "install ffmpeg"
        raise ValueError("not an image (and no ffmpeg to try it as a video)")
    try:
        meta = subprocess.run(
            [ffprobe, "-v", "error", *FFMPEG_SAFE, "-select_streams", "v:0", "-show_entries",
             "stream=width,height:stream_side_data=rotation:stream_tags=rotate:format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, check=False, timeout=FFMPEG_TIMEOUT,
        )  # fmt: skip
        info = json.loads(meta.stdout or "{}")
        stream = info["streams"][0]
        w, h = int(stream["width"]), int(stream["height"])
    except subprocess.TimeoutExpired as e:
        raise ValueError("ffprobe timed out") from e
    except (KeyError, IndexError, ValueError, TypeError) as e:
        raise ValueError("not an image or video") from e
    if _rotation(stream) % 180 == 90:  # ffmpeg rotates the frames, so the layout must too
        w, h = h, w
    try:
        duration: float | None = float(info["format"]["duration"])
    except (KeyError, ValueError, TypeError):
        duration = None
    # Poster 10% in, past the black frames many clips open with
    poster = workdir / "poster.png"
    seek = f"{(duration or 0) * 0.1:.2f}"
    try:
        subprocess.run(
            [ffmpeg, "-v", "error", "-nostdin", "-y", *FFMPEG_SAFE, "-ss", seek, "-i", str(path),
             "-frames:v", "1", str(poster)],
            capture_output=True, check=False, timeout=FFMPEG_TIMEOUT,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        pass
    return Media(path, "video", w, h, poster if poster.exists() else None, duration=duration)


def _rotation(stream: dict) -> int:
    for side in stream.get("side_data_list", []):
        if "rotation" in side:
            return abs(int(float(side["rotation"])))
    try:
        return abs(int(stream.get("tags", {}).get("rotate", 0)))
    except ValueError:
        return 0


def scaled_png(path: Path, width: int) -> bytes:
    """PNG bytes of `path` at most `width` px wide, for inline transfer (keeps SSH traffic to what
    the pane can actually show). Called off the event loop; the pane caches the results."""
    with Image.open(path) as im:
        if im.width > width > 0:
            im = im.resize(
                (width, max(1, round(im.height * width / im.width))), Image.Resampling.LANCZOS
            )
        buf = io.BytesIO()
        im.save(buf, format="PNG", compress_level=3)
        return buf.getvalue()


def cropped_png(path: Path, out_w: int, out_h: int, zoom: float, cx: float, cy: float) -> bytes:
    """The part of `path` a zoomed view shows (1/zoom of each side, centred at fraction cx, cy),
    scaled to out_w x out_h px. Runs on a worker; the pane caches the result."""
    with Image.open(path) as im:
        w, h = im.size
        rw, rh = w / zoom, h / zoom
        x0 = min(max(cx * w - rw / 2, 0), w - rw)
        y0 = min(max(cy * h - rh / 2, 0), h - rh)
        box = (round(x0), round(y0), round(x0 + rw), round(y0 + rh))
        region = im.crop(box).resize((max(1, out_w), max(1, out_h)), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        region.save(buf, format="PNG", compress_level=1)
        return buf.getvalue()


def sniff_name(data: bytes) -> str:
    """A file name with the right extension for media of unknown origin (e.g. stdin)."""
    if data.startswith(b"\x89PNG"):
        return "stdin.png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "stdin.gif"
    if data.startswith(b"\xff\xd8"):
        return "stdin.jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "stdin.webp"
    if data[4:8] == b"ftyp":
        return "stdin.mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "stdin.webm"
    return "stdin"
