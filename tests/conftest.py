from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image


@pytest.fixture
def png(tmp_path: Path) -> Path:
    p = tmp_path / "plot.png"
    Image.new("RGB", (300, 200), (200, 40, 40)).save(p)
    return p


@pytest.fixture
def gif(tmp_path: Path) -> Path:
    p = tmp_path / "anim.gif"
    frames = [Image.new("RGB", (64, 48), (i * 40, 0, 255 - i * 40)) for i in range(6)]
    frames[0].save(p, save_all=True, append_images=frames[1:], duration=50, loop=0)
    return p


@pytest.fixture
def mp4(tmp_path: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not (ffmpeg and shutil.which("ffprobe")):
        pytest.skip("needs ffmpeg")
    p = tmp_path / "clip.mp4"
    subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25",
         "-t", "2", "-pix_fmt", "yuv420p", str(p)],
        check=True,
    )  # fmt: skip
    return p
