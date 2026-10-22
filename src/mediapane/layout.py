"""Pure layout: fitting images to cells, the gallery's line map, and input parsing."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .kitty import MAX_CELLS

MAX_UPSCALE = 2.0


def fit(
    w: int,
    h: int,
    max_cols: int,
    max_rows: int,
    cell_w: float,
    cell_h: float,
    upscale: float = MAX_UPSCALE,
) -> tuple[int, int]:
    """Cells (cols, rows) for a w x h px image within max_cols x max_rows, keeping its aspect
    ratio and enlarging at most `upscale` times. (0, 0) if it can't be shown."""
    max_cols, max_rows = min(max_cols, MAX_CELLS), min(max_rows, MAX_CELLS)
    if w <= 0 or h <= 0 or max_cols < 1 or max_rows < 1:
        return 0, 0
    s = min(upscale, max_cols * cell_w / w, max_rows * cell_h / h)
    cols = max(1, min(max_cols, round(w * s / cell_w)))
    rows = max(1, min(max_rows, round(h * s / cell_h)))
    return cols, rows


@dataclass(frozen=True)
class Line:
    item: int  # index into the item list
    kind: str  # "gap" | "caption" | "image"
    row: int = 0  # image row, for kind == "image"


def gallery_lines(sizes: list[tuple[int, int]]) -> tuple[list[Line], list[int]]:
    """The gallery as lines: a gap, a caption, then one line per image row, for each item.
    `sizes` are each item's (cols, rows). Returns the lines and each item's first line."""
    lines: list[Line] = []
    starts: list[int] = []
    for i, (_, rows) in enumerate(sizes):
        starts.append(len(lines))
        lines.append(Line(i, "gap"))
        lines.append(Line(i, "caption"))
        lines.extend(Line(i, "image", r) for r in range(rows))
    return lines, starts


# ---- text from outside: names and hosts arrive from pushers, possibly another machine ------

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_ESCAPES = re.compile(r"\x1b\[[\d;?]*[A-Za-z]")


def clean(text: object, limit: int = 200) -> str:
    """Make untrusted text safe to print: no control characters (so no escape sequences reach the
    terminal), no lone surrogates (undecodable file names), bounded length."""
    s = str(text).encode("utf-8", "replace").decode("utf-8")
    return _CONTROL.sub("\N{REPLACEMENT CHARACTER}", s)[:limit]


def char_width(ch: str) -> int:
    """Terminal cells taken by one character: 2 for wide (CJK, most emoji), 0 for combining."""
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def width(text: str) -> int:
    """Cells `text` takes on screen, ignoring SGR escape sequences."""
    return sum(char_width(ch) for ch in _ESCAPES.sub("", text))


def clip(text: str, cells: int) -> str:
    """Cut `text` to at most `cells` cells, keeping escape sequences intact; ends with a reset."""
    out, used, i = [], 0, 0
    while i < len(text):
        m = _ESCAPES.match(text, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        w = char_width(text[i])
        if used + w > cells:
            break
        out.append(text[i])
        used += w
        i += 1
    return "".join(out) + "\x1b[0m"


# ---- input -----------------------------------------------------------------------------------
# Keys come in as single characters or names: up down left right pgup pgdn home end enter esc
# backspace tab btab focus_in focus_out paste_start paste_end; mouse events as
# ("mouse", button, col, row, pressed)

_CSI = re.compile(r"\x1b(?:\[<(\d+);(\d+);(\d+)([Mm])|\[([\d;]*)([A-Za-z~])|O([A-Za-z]))")
_PARTIAL = re.compile(r"\x1b(?:\[[<\d;]*|O)?")  # a sequence cut off by the end of a read
_FINAL = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end", "Z": "btab"}
_TILDE = {
    "1": "home", "7": "home", "4": "end", "8": "end", "5": "pgup", "6": "pgdn", "3": "del",
    "200": "paste_start", "201": "paste_end",
}  # fmt: skip
_CTRL = {"\r": "enter", "\n": "enter", "\x7f": "backspace", "\x08": "backspace", "\t": "tab"}


def parse_keys(text: str) -> tuple[list[str | tuple], str]:
    """Keys in `text`, and an unfinished escape sequence at its end (to prepend to the next
    read; a lone ESC that stays alone is the Esc key, which the caller decides after a pause)."""
    out: list[str | tuple] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\x1b":
            m = _CSI.match(text, i)
            if m is None:
                partial = _PARTIAL.fullmatch(text, i)
                if partial:
                    return out, text[i:]
                out.append("esc")
                i += 1
                continue
            i = m.end()
            if m.group(1) is not None:
                button, col, row = int(m.group(1)), int(m.group(2)) - 1, int(m.group(3)) - 1
                out.append(("mouse", button, col, row, m.group(4) == "M"))
            elif m.group(6) == "~":
                if key := _TILDE.get((m.group(5) or "").split(";")[0]):
                    out.append(key)
            elif m.group(6) in ("I", "O") and not m.group(5):
                out.append("focus_in" if m.group(6) == "I" else "focus_out")
            elif key := _FINAL.get(m.group(6) or m.group(7) or ""):
                out.append(key)
            continue
        out.append(_CTRL.get(ch, ch))
        i += 1
    return out, ""
