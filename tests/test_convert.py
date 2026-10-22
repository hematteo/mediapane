import io

import numpy as np
import pytest
from PIL import Image

from mediapane.convert import grid, to_media


def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


def test_path_and_bytes(png):
    data, name = to_media(png)
    assert name == "plot.png" and data == png.read_bytes()
    data, name = to_media(png.read_bytes())
    assert name == "stdin.png"


def test_hwc_chw_and_gray_arrays():
    assert _open(to_media(np.zeros((20, 30, 3), np.uint8))[0]).size == (30, 20)
    assert _open(to_media(np.zeros((3, 20, 30), np.float32))[0]).size == (30, 20)
    im = _open(to_media(np.zeros((20, 30), np.float32))[0])
    assert im.size == (30, 20) and im.mode == "L"


def test_float_ranges():
    x = np.linspace(0, 1, 100, dtype=np.float32).reshape(10, 10)
    assert np.asarray(_open(to_media(x)[0])).max() == 255
    y = np.full((4, 4), np.nan, dtype=np.float32)
    assert np.asarray(_open(to_media(y)[0])).max() == 0  # NaN -> 0, no crash
    z = np.linspace(-3, 7, 16, dtype=np.float32).reshape(4, 4)  # arbitrary range: min-max
    arr = np.asarray(_open(to_media(z)[0]))
    assert arr.min() == 0 and arr.max() == 255


def test_batch_becomes_grid():
    batch = np.ones((5, 3, 8, 10), np.float32)  # NCHW
    im = _open(to_media(batch)[0])
    # 5 images -> 3 columns x 2 rows, 2 px gaps
    assert im.size == (3 * 10 + 2 * 2, 2 * 8 + 1 * 2)


def test_frames_become_animation():
    frames = np.random.default_rng(0).integers(0, 255, (6, 16, 16, 3), dtype=np.uint8)
    data, name = to_media(frames, fps=12)
    im = _open(data)
    assert name == "frames.png" and im.n_frames == 6


def test_grid_shapes():
    out = grid([np.zeros((4, 4))] * 4)
    assert out.shape == (10, 10)


def test_rejects_unknown():
    with pytest.raises(TypeError):
        to_media(object())


def test_matplotlib_figure():
    matplotlib = pytest.importorskip("matplotlib")
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots()
    ax.plot([0, 1], [0, 1])
    data, name = to_media(fig)
    plt.close(fig)
    assert name == "figure.png" and data.startswith(b"\x89PNG")


def test_gray_frame_stack_with_fps():
    frames = np.stack([np.full((8, 8), t, np.float32) for t in range(5)])  # (T, H, W)
    im = _open(to_media(frames, fps=5)[0])
    assert im.n_frames == 5 and im.size == (8, 8)
    im.seek(4)
    assert np.asarray(im.convert("L")).max() == 255  # shared range across frames


def test_signed_unit_range():
    x = np.array([[-1.0, 0.0, 1.0]], np.float32)
    assert list(np.asarray(_open(to_media(x)[0]))[0]) == [0, 128, 255]
