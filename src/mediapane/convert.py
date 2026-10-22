"""Turn Python objects into media bytes: files, PIL images, matplotlib figures, NumPy arrays and
torch tensors (single images, batches tiled into a grid, or frame stacks as animations)."""

from __future__ import annotations

import io
import math
import os
from pathlib import Path
from typing import Any

from PIL import Image


def to_media(
    obj: Any, name: str | None = None, *, fps: float | None = None, video: bool = False
) -> tuple[bytes, str]:
    """(data, file name) for anything `mediapane.show` accepts. A 4-D array is a batch of images
    by default; pass `fps` or `video=True` to treat the first axis of a 3-D or 4-D array as
    time instead."""
    if isinstance(obj, (str, os.PathLike)):
        p = Path(obj)
        return p.read_bytes(), name or p.name
    if isinstance(obj, (bytes, bytearray, memoryview)):
        from .media import sniff_name

        data = bytes(obj)
        return data, name or sniff_name(data)
    if isinstance(obj, Image.Image):
        return _png(obj), name or "image.png"
    if hasattr(obj, "savefig"):  # matplotlib Figure
        buf = io.BytesIO()
        obj.savefig(buf, format="png", bbox_inches="tight")
        return buf.getvalue(), name or "figure.png"
    arr = _as_numpy(obj)
    if arr is None:
        raise TypeError(f"can't show {type(obj).__name__}")
    if arr.ndim in (3, 4) and (video or fps):  # (T, H, W[, C]) or (T, C, H, W)
        arr = _to_uint8(arr)  # one range for all frames, so brightness doesn't flicker
        frames = [Image.fromarray(_hwc(f)) for f in arr]
        return _apng(frames, fps or 10.0), name or "frames.png"
    if arr.ndim == 4:
        return _png(Image.fromarray(_to_uint8(grid([_hwc(x) for x in arr])))), name or "batch.png"
    if arr.ndim == 3 and arr.shape[-1] not in (1, 3, 4) and arr.shape[0] not in (1, 3, 4):
        return _png(Image.fromarray(_to_uint8(grid(list(arr))))), name or "batch.png"  # (N, H, W)
    if arr.ndim in (2, 3):
        return _png(Image.fromarray(_to_uint8(_hwc(arr)))), name or "array.png"
    raise ValueError(f"can't show an array of shape {tuple(arr.shape)}")


def _as_numpy(obj: Any) -> Any:
    if hasattr(obj, "detach"):  # torch tensor, without importing torch
        obj = obj.detach().cpu()
        if str(getattr(obj, "dtype", "")) == "torch.bfloat16":
            obj = obj.float()
        obj = obj.numpy()
    if hasattr(obj, "__array__"):
        import numpy as np

        return np.asarray(obj)
    return None


def _hwc(x: Any) -> Any:
    """(H, W), (H, W, C) or (C, H, W) -> (H, W) / (H, W, C) with C in {3, 4}."""
    if x.ndim == 3 and x.shape[0] in (1, 3, 4) and x.shape[-1] not in (1, 3, 4):
        x = x.transpose(1, 2, 0)  # (C, H, W) -> (H, W, C)
    if x.ndim == 3 and x.shape[-1] == 1:
        x = x[..., 0]
    return x


def _to_uint8(x: Any) -> Any:
    import numpy as np

    if x.dtype == np.uint8:
        return x
    if x.dtype == bool:
        return x.astype(np.uint8) * 255
    x = x.astype(np.float32)
    x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    lo, hi = float(x.min()), float(x.max())
    if lo >= 0.0 and hi <= 1.0:
        x = x * 255.0  # [0, 1]
    elif lo >= -1.0 and hi <= 1.0:
        x = (x + 1.0) * 127.5  # [-1, 1], e.g. normalised inputs, generator outputs
    else:
        x = (x - lo) / (hi - lo or 1.0) * 255.0  # anything else: stretch to the full range
    return np.clip(x + 0.5, 0, 255).astype(np.uint8)


def grid(images: list[Any], pad: int = 2) -> Any:
    """Tile same-sized (H, W[, C]) arrays into a near-square grid with `pad` px gaps."""
    import numpy as np

    n = len(images)
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    h, w = images[0].shape[:2]
    extra = images[0].shape[2:]
    out = np.zeros(
        (rows * h + (rows - 1) * pad, cols * w + (cols - 1) * pad, *extra), dtype=images[0].dtype
    )
    for i, im in enumerate(images):
        r, c = divmod(i, cols)
        out[r * (h + pad) : r * (h + pad) + h, c * (w + pad) : c * (w + pad) + w] = im
    return out


def _png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="PNG", compress_level=3)
    return buf.getvalue()


def _apng(frames: list[Image.Image], fps: float) -> bytes:
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
