"""Push a little of everything to a running pane: a matplotlib figure, a batch of samples as a
grid, a frame stack as an animation, and (with ffmpeg) a short video.

    mediapane open                      # in Ghostty / kitty / tmux / WezTerm
    uv run python examples/demo.py
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import matplotlib
import numpy as np

import mediapane

matplotlib.use("module://mediapane.mpl")
import matplotlib.pyplot as plt  # noqa: E402

rng = np.random.default_rng(0)

# 1. A training curve through the matplotlib backend
steps = np.arange(3000)
fig, ax = plt.subplots(figsize=(7, 3.6), num="training loss")
for lr, color in [(3e-4, "#205ea6"), (1e-3, "#bc5215")]:
    tau = 900 if lr == 3e-4 else 500
    loss = 2.4 * np.exp(-steps / tau) + 0.35 + 0.04 * rng.standard_normal(steps.size)
    ax.plot(steps, loss, lw=1, color=color, label=f"lr={lr:g}")
ax.set(xlabel="step", ylabel="loss")
ax.legend(frameon=False)
plt.show()
time.sleep(0.5)

# 2. A batch of "samples" (N, C, H, W) floats in [0, 1] -> one grid image
yy, xx = np.mgrid[0:64, 0:64] / 64
batch = np.stack(
    [
        np.stack([np.sin(6 * xx + k) ** 2, np.cos(5 * yy - k) ** 2, (xx * yy + k / 16) % 1])
        for k in range(16)
    ]
).astype(np.float32)
mediapane.show(batch, name="samples step 3000")
time.sleep(0.5)

# 3. A (T, H, W) stack -> animation, e.g. how a map evolves over training
frames = np.stack([np.sin(8 * xx + t / 3) * np.cos(8 * yy - t / 5) for t in range(48)])
mediapane.show(frames, name="activation over time", fps=16)
time.sleep(0.5)

# 4. A video file, if ffmpeg is around
if ffmpeg := shutil.which("ffmpeg"):
    clip = Path(tempfile.mkdtemp()) / "rollout.mp4"
    subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "mandelbrot=size=480x270:rate=25",
         "-t", "5", "-pix_fmt", "yuv420p", str(clip)],
        check=True,
    )  # fmt: skip
    mediapane.push(clip)

# 5. A series: one entry, stepped with ←/→, time-lapse with t
for upto in range(300, 3001, 300):
    fig, ax = plt.subplots(figsize=(7, 3.6), num=f"loss@{upto}")
    s = steps[:upto]
    ax.plot(s, 2.4 * np.exp(-s / 700) + 0.35 + 0.04 * rng.standard_normal(s.size), lw=1)
    ax.set(xlim=(0, 3000), ylim=(0, 3), xlabel="step", ylabel="loss", title=f"step {upto}")
    mediapane.show(fig, tag="training loss", step=upto)
    plt.close(fig)
