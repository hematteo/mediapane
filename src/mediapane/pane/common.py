"""Constants shared by the pane's modules."""

from __future__ import annotations

import os

from .. import term
from ..formats import IMAGE_EXTS, VIDEO_EXTS
from ..plugins import CORE_KEYS  # noqa: F401 - re-exported

OFF, DIM, BOLD = "\x1b[0m", "\x1b[2m", "\x1b[1m"
BADGE, BADGE_SEL, ACCENT = "\x1b[2;7m", "\x1b[7;34m", "\x1b[35m"
SYNC_ON, SYNC_OFF = "\x1b[?2026h", "\x1b[?2026l"  # synchronized output: no half-drawn frames
# alt screen, no cursor, SGR mouse, focus events, bracketed paste
SCREEN_ON = "\x1b[?1049h\x1b[?25l\x1b[?1000h\x1b[?1006h\x1b[?1004h\x1b[?2004h"
SCREEN_OFF = "\x1b[?2004l\x1b[?1004l\x1b[?1006l\x1b[?1000l\x1b[?25h\x1b[?1049l"
MAX_PAYLOAD = 4 << 30
MAX_ITEMS = int(os.environ.get("MEDIAPANE_MAX_ITEMS", "500"))  # oldest are dropped beyond this
ID_SPACE = 255 * 256  # image ids make_id can encode
SCALED_CACHE = 64 << 20  # bytes of pre-scaled PNGs kept for inline transfer
MESSAGE_SECS = 4.0
DOUBLE_CLICK = 0.4
# A lone ESC this long without a follow-up is the Esc key (longer over SSH, where the rest of a
# mouse or arrow sequence can arrive in a later packet)
ESC_WAIT = 0.3 if term.over_ssh() else 0.05
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS
LAPSE_FPS = 4.0  # time-lapse speed through a series
MAX_ZOOM = 8.0
NATIVE_MAX_FRAMES = 300  # longer animations aren't worth storing in the terminal
NATIVE_MAX_PIXELS = 150e6  # nor ones whose frames add up to more pixels than this
HISTORY_MB = float(os.environ.get("MEDIAPANE_HISTORY_MB", "1024"))  # kept for --resume; 0: off

HELP = [
    ("j k  ↓ ↑  wheel", "scroll"),
    ("n p  tab", "next / previous item"),
    ("1-9", "select item"),
    ("← →  , .  < >", "step through a series (first / last)"),
    ("t", "time-lapse through a series"),
    ("g G", "top / bottom (G follows new items)"),
    ("enter  double-click", "view one item large, esc to go back"),
    ("+ - 0", "zoom in the large view (arrows / hjkl pan)"),
    ("space", "play / pause"),
    ("r", "replay"),
    ("o", "open in the default app (local files only)"),
    ("x", "remove item"),
    ("C", "clear everything"),
    ("q", "quit"),
]
