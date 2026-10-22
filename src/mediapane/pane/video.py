"""Video and frame transport: ffmpeg streams, shared memory, terminal capabilities."""

from __future__ import annotations

import os
import re
import signal
import struct
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

from ..kitty import Graphics
from ..media import FFMPEG_SAFE, scaled_png


class PngStream:
    """Splits ffmpeg's image2pipe output into whole PNGs (signature, then chunks up to IEND)."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.pos = 8  # next chunk header in buf (resumes across reads)

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        out = []
        while (end := self._end()) is not None:
            out.append(bytes(self.buf[:end]))
            del self.buf[:end]
            self.pos = 8
        return out

    def _end(self) -> int | None:
        while self.pos + 8 <= len(self.buf):
            (length,) = struct.unpack_from(">I", self.buf, self.pos)
            kind = bytes(self.buf[self.pos + 4 : self.pos + 8])
            if self.pos + 12 + length > len(self.buf):
                return None  # chunk not complete yet; resume here next time
            self.pos += 12 + length
            if kind == b"IEND":
                return self.pos
        return None


class VideoPlayer:
    """ffmpeg decoding one video in real time (-re) to a PNG stream at the displayed width."""

    def __init__(
        self, ffmpeg: str, path: Path, start: float, width: int, fps: int, level: int, log: Path
    ) -> None:
        cap = ["-fpsmax", str(fps)] if _ffmpeg_has_fpsmax(ffmpeg) else []
        self.log = log
        with open(log, "wb") as err:
            self.proc = subprocess.Popen(
                [ffmpeg, "-v", "error", "-nostdin", "-re", *FFMPEG_SAFE, "-ss", f"{start:.3f}",
                 "-i", str(path),
                 "-an", "-vf", f"scale={width}:-2", *cap, "-c:v", "png",
                 "-compression_level", str(level), "-f", "image2pipe", "-"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err,
            )  # fmt: skip
        assert self.proc.stdout is not None
        self.fd = self.proc.stdout.fileno()
        os.set_blocking(self.fd, False)
        self.stream = PngStream()
        self.start = start
        self.started = time.monotonic()
        self.width = width
        self.frames = 0

    def stop(self) -> float:
        """Stop decoding; returns the position reached in seconds."""
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        if self.proc.stdout:
            self.proc.stdout.close()
        return self.start + time.monotonic() - self.started

    def error(self) -> str:
        """ffmpeg's last complaint, if it failed."""
        if self.proc.returncode in (0, None, -signal.SIGKILL):
            return ""
        try:
            lines = self.log.read_text(errors="replace").strip().splitlines()
        except OSError:
            lines = []
        return lines[-1] if lines else f"ffmpeg exited with {self.proc.returncode}"


class FrameSink:
    """Hands video frames to a local terminal without touching the disk when possible: POSIX
    shared memory (t=s; the terminal unlinks each segment after reading it), else PNG files
    written atomically in a rotation of three."""

    def __init__(self, gfx: Graphics, session: Path, shm: bool) -> None:
        self.gfx, self.session, self.shm = gfx, session, shm
        self.count = 0
        self.names: deque[str] = deque()

    def send(self, image_id: int, cells: tuple[int, int], png: bytes) -> str:
        c, r = cells
        self.count += 1
        if self.shm:
            name = f"mp{os.getpid()}-{self.count}"
            try:
                _shm_write(name, png)
            except OSError:
                self.shm = False  # out of shm space or not allowed: fall back to files
            else:
                self.names.append(name)
                # The terminal unlinks segments it read; reap any it didn't (e.g. while hidden)
                while len(self.names) > 16:
                    _shm_unlink(self.names.popleft())
                return self.gfx.place_shm(image_id, c, r, _shm_path(name), len(png))
        path = self.session / f"v{image_id}-{self.count % 3}.png"
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(png)
        os.replace(tmp, path)  # the terminal never sees a half-written file
        return self.gfx.place(image_id, c, r, path=path)

    def close(self) -> None:
        while self.names:
            _shm_unlink(self.names.popleft())


_FPSMAX: dict[str, bool] = {}


def _ffmpeg_has_fpsmax(ffmpeg: str) -> bool:
    """-fpsmax arrived in FFmpeg 4.4; older builds (Debian 11, Ubuntu 20.04) reject it."""
    if ffmpeg not in _FPSMAX:
        try:
            out = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, timeout=10)
            m = re.search(r"version n?(\d+)\.(\d+)", out.stdout)
            _FPSMAX[ffmpeg] = m is None or (int(m[1]), int(m[2])) >= (4, 4)  # git builds: new
        except (OSError, subprocess.TimeoutExpired):
            _FPSMAX[ffmpeg] = False
    return _FPSMAX[ffmpeg]


def _shm_write(name: str, data: bytes) -> None:
    """Put `data` in a new POSIX shared memory segment `name`, left for the terminal to unlink."""
    from multiprocessing import shared_memory

    if sys.version_info >= (3, 13):
        shm = shared_memory.SharedMemory(name=name, create=True, size=len(data), track=False)
    else:
        shm = shared_memory.SharedMemory(name=name, create=True, size=len(data))
        from multiprocessing import resource_tracker

        # Otherwise Python unlinks it at exit and warns about a "leak" the terminal cleans up
        resource_tracker.unregister(shm._name, "shared_memory")  # type: ignore[attr-defined]
    try:
        shm.buf[: len(data)] = data
    finally:
        shm.close()


def _shm_path(name: str) -> str:
    """The POSIX name of segment `name`: SharedMemory adds the leading slash, so the terminal
    must be told it too."""
    return "/" + name


def _shm_unlink(name: str) -> None:
    try:
        import _posixshmem  # CPython's own binding, used by multiprocessing.shared_memory

        _posixshmem.shm_unlink(_shm_path(name))
    except (ImportError, OSError):
        pass


def _tiny_png() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (1, 1)).save(buf, format="PNG")
    return buf.getvalue()


def _scale_all(paths: list[Path], px: int) -> list[bytes]:
    return [scaled_png(p, px) for p in paths]
