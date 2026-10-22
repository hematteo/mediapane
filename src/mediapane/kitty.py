"""Kitty graphics protocol output: image transmission, Unicode placeholders, tmux passthrough.

Images are shown through Unicode placeholders: text cells (U+10EEEE plus row/column diacritics,
with the image id carried by the foreground colour) that the terminal replaces with the image.
Being text, they scroll with everything else and pass through tmux; re-sending data under the
same id updates every placeholder for it, which is how animations play without moving the cursor.
Spec: https://sw.kovidgoyal.net/kitty/graphics-protocol/#unicode-placeholders
"""

from __future__ import annotations

import base64
import os

PLACEHOLDER = "\U0010eeee"

# Row/column diacritics, from kitty's gen/rowcolumn-diacritics.txt (v0.39.1)
_DIACRITIC_CODES = """
0305 030D 030E 0310 0312 033D 033E 033F 0346 034A 034B 034C 0350 0351 0352 0357 035B 0363 0364
0365 0366 0367 0368 0369 036A 036B 036C 036D 036E 036F 0483 0484 0485 0486 0487 0592 0593 0594
0595 0597 0598 0599 059C 059D 059E 059F 05A0 05A1 05A8 05A9 05AB 05AC 05AF 05C4 0610 0611 0612
0613 0614 0615 0616 0617 0657 0658 0659 065A 065B 065D 065E 06D6 06D7 06D8 06D9 06DA 06DB 06DC
06DF 06E0 06E1 06E2 06E4 06E7 06E8 06EB 06EC 0730 0732 0733 0735 0736 073A 073D 073F 0740 0741
0743 0745 0747 0749 074A 07EB 07EC 07ED 07EE 07EF 07F0 07F1 07F3 0816 0817 0818 0819 081B 081C
081D 081E 081F 0820 0821 0822 0823 0825 0826 0827 0829 082A 082B 082C 082D 0951 0953 0954 0F82
0F83 0F86 0F87 135D 135E 135F 17DD 193A 1A17 1A75 1A76 1A77 1A78 1A79 1A7A 1A7B 1A7C 1B6B 1B6D
1B6E 1B6F 1B70 1B71 1B72 1B73 1CD0 1CD1 1CD2 1CDA 1CDB 1CE0 1DC0 1DC1 1DC3 1DC4 1DC5 1DC6 1DC7
1DC8 1DC9 1DCB 1DCC 1DD1 1DD2 1DD3 1DD4 1DD5 1DD6 1DD7 1DD8 1DD9 1DDA 1DDB 1DDC 1DDD 1DDE 1DDF
1DE0 1DE1 1DE2 1DE3 1DE4 1DE5 1DE6 1DFE 20D0 20D1 20D4 20D5 20D6 20D7 20DB 20DC 20E1 20E7 20E9
20F0 2CEF 2CF0 2CF1 2DE0 2DE1 2DE2 2DE3 2DE4 2DE5 2DE6 2DE7 2DE8 2DE9 2DEA 2DEB 2DEC 2DED 2DEE
2DEF 2DF0 2DF1 2DF2 2DF3 2DF4 2DF5 2DF6 2DF7 2DF8 2DF9 2DFA 2DFB 2DFC 2DFD 2DFE 2DFF A66F A67C
A67D A6F0 A6F1 A8E0 A8E1 A8E2 A8E3 A8E4 A8E5 A8E6 A8E7 A8E8 A8E9 A8EA A8EB A8EC A8ED A8EE A8EF
A8F0 A8F1 AAB0 AAB2 AAB3 AAB7 AAB8 AABE AABF AAC1 FE20 FE21 FE22 FE23 FE24 FE25 FE26 10A0F
10A38 1D185 1D186 1D187 1D188 1D189 1D1AA 1D1AB 1D1AC 1D1AD 1D242 1D243 1D244
"""
DIACRITICS = [chr(int(c, 16)) for c in _DIACRITIC_CODES.split()]
MAX_CELLS = len(DIACRITICS)  # rows/columns one placement can address (297)
CHUNK = 4096  # protocol limit on base64 payload per escape


def make_id(n: int) -> int:
    """The n-th image id (n >= 0). The low byte goes in a 256-colour foreground, which tmux keeps
    intact even without truecolour support; the top byte goes in a third diacritic."""
    hi, lo = divmod(n, 255)
    if hi > 255:
        raise ValueError("out of image ids")
    return hi << 24 | (lo + 1)


def apc(control: str, payload: str = "") -> str:
    return f"\x1b_G{control};{payload}\x1b\\" if payload else f"\x1b_G{control}\x1b\\"


def tmux_wrap(seq: str) -> str:
    """Pass an escape sequence through tmux to the outer terminal (needs allow-passthrough)."""
    return "\x1bPtmux;" + seq.replace("\x1b", "\x1b\x1b") + "\x1b\\"


def placeholder_row(image_id: int, row: int, cols: int) -> str:
    """One row of placeholder cells showing row `row` of image `image_id` across `cols` cells."""
    lo, hi = image_id & 0xFF, image_id >> 24 & 0xFF
    tail = DIACRITICS[hi] if hi else ""
    r = DIACRITICS[row]
    cells = "".join(f"{PLACEHOLDER}{r}{DIACRITICS[c]}{tail}" for c in range(cols))
    return f"\x1b[38;5;{lo}m{cells}\x1b[39m"


class Graphics:
    """Builds graphics escapes for one terminal: PNGs by file path when the terminal can read our
    files, otherwise inline base64 data; wrapped for tmux when running inside it."""

    def __init__(self, *, direct: bool, tmux: bool) -> None:
        self.direct = direct
        self.tmux = tmux

    def _esc(self, seq: str) -> str:
        return tmux_wrap(seq) if self.tmux else seq

    def place(
        self,
        image_id: int,
        cols: int,
        rows: int,
        *,
        path: str | os.PathLike[str] | None = None,
        png: bytes | None = None,
    ) -> str:
        """Upload a PNG as `image_id` with a virtual placement of cols x rows cells. Re-sending
        under an existing id replaces the image wherever its placeholders are."""
        ctl = f"a=T,U=1,q=2,f=100,i={image_id},c={cols},r={rows}"
        if path is not None and not self.direct:
            return self._esc(apc(f"{ctl},t=f", _b64(os.fsencode(path))))
        if png is None:
            if path is None:
                raise ValueError("need a path or PNG bytes")
            with open(path, "rb") as f:
                png = f.read()
        return self.data(ctl, png)

    def place_shm(self, image_id: int, cols: int, rows: int, name: str, size: int) -> str:
        """Upload a PNG the terminal reads from POSIX shared memory `name` (and then unlinks)."""
        ctl = f"a=T,U=1,q=2,f=100,i={image_id},c={cols},r={rows},t=s,S={size}"
        return self._esc(apc(ctl, _b64(name.encode())))

    def data(self, control: str, payload: bytes) -> str:
        """Inline transfer (t=d), split into protocol-sized chunks."""
        data = _b64(payload)
        chunks = [data[i : i + CHUNK] for i in range(0, len(data), CHUNK)] or [""]
        out = []
        for k, chunk in enumerate(chunks):
            more = int(k < len(chunks) - 1)
            ctl = f"{control},t=d,m={more}" if k == 0 else f"q=2,m={more}"
            out.append(self._esc(apc(ctl, chunk)))
        return "".join(out)

    def frame(self, image_id: int, png: bytes, gap_ms: int) -> str:
        """Add an animation frame to image `image_id`, shown for `gap_ms` before the next."""
        return self.data(f"a=f,q=2,i={image_id},f=100,z={gap_ms}", png)

    def animate(self, image_id: int, **keys: int) -> str:
        """Control an image's animation: s=1 stop, s=3 run (v=1: loop forever); r+z set a
        frame's gap."""
        ctl = ",".join(f"{k}={v}" for k, v in keys.items())
        return self._esc(apc(f"a=a,q=2,i={image_id},{ctl}"))

    def inline(self, png: bytes, cols: int) -> str:
        """A plain placement at the cursor, `cols` cells wide (for one-off output, no pane)."""
        return self.data(f"a=T,q=2,f=100,c={cols}", png)

    def delete_all(self) -> str:
        return self._esc(apc("a=d,d=A,q=2"))

    def delete(self, image_id: int) -> str:
        return self._esc(apc(f"a=d,d=I,i={image_id},q=2"))


def _b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode("ascii")
