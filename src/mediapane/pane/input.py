"""Keys and mouse, plugin keys, opening items externally."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

from .. import plugins, term
from ..layout import clean, parse_keys
from .common import CORE_KEYS, DOUBLE_CLICK, MEDIA_EXTS
from .model import Item

_PAN = {"left": (-0.25, 0), "h": (-0.25, 0), "right": (0.25, 0), "l": (0.25, 0),
        "up": (0, -0.25), "k": (0, -0.25), "down": (0, 0.25), "j": (0, 0.25)}  # fmt: skip


class InputMixin:
    def on_input(self, _fd: object) -> None:
        try:
            data = os.read(self.fd_in, 4096)
        except OSError:
            data = b""
        if not data:
            self.running = False
            return
        keys, self.inbuf = parse_keys(self.inbuf + self.decoder.decode(data))
        if self.inbuf == "\x1b":
            self.esc_at = time.monotonic()
        for key in keys:
            self.handle(key)

    def handle(self, key: str | tuple) -> None:
        self.fresh_layout()  # keys act on what's on screen: items may have changed this round
        if key == "paste_start":
            self.pasting = True
        elif key == "paste_end":
            self.pasting = False
        if self.pasting or key in ("paste_start", "paste_end", "focus_out"):
            return  # pasted text isn't commands
        if key == "focus_in":
            if self.tmux:  # tmux drops passthrough while a pane is hidden: re-send everything
                self.sent.clear()
                self.dirty = True
            return
        if self.help:
            self.help, self.dirty = False, True
            return
        if isinstance(key, tuple):
            self.handle_mouse(*key[1:])
            return
        n = len(self.items)
        sel = self.items[self.selected] if 0 <= self.selected < n else None
        if self.api and key in self.api.keys and key not in CORE_KEYS:
            self.run_key(self.api.keys[key])
            return
        if self.focus and sel and self.zoom > 1 and key in _PAN:
            self.pan(*_PAN[key])
            return
        if self.focus and sel and key in ("+", "=", "-", "0"):
            if key == "0":
                self.zoom, self.center, self.dirty = 1.0, (0.5, 0.5), True
            else:
                self.zoom_by(1.5 if key in ("+", "=") else 1 / 1.5)
            return
        if sel and sel.steps and key in ("left", "right", ",", ".", "<", ">", "t"):
            last = len(sel.steps) - 1
            if key == "t":
                self.toggle_lapse(sel)
            else:
                k = {"left": sel.cur - 1, ",": sel.cur - 1, "right": sel.cur + 1, ".": sel.cur + 1,
                     "<": 0, ">": last}[key]  # fmt: skip
                self.step(sel, max(0, min(last, k)))
            return
        if key == "q":
            self.running = False
        elif key == "?":
            self.help, self.dirty = True, True
        elif key in ("j", "down"):
            self.scroll(1)
        elif key in ("k", "up"):
            self.scroll(-1)
        elif key in ("pgdn", "\x06"):
            self.scroll(self.view - 1)
        elif key in ("pgup", "\x02"):
            self.scroll(-(self.view - 1))
        elif key == "\x04":
            self.scroll(self.view // 2)
        elif key == "\x15":
            self.scroll(-(self.view // 2))
        elif key in ("n", "tab", "J", "right") and n:
            self.select(min(n - 1, self.selected + 1))
        elif key in ("p", "btab", "K", "left") and n:
            self.select(max(0, self.selected - 1))
        elif key in ("g", "home") and n:
            self.select(0)
        elif key in ("G", "end") and n:
            self.follow = True
            self.select(n - 1)
        elif len(key) == 1 and key in "123456789" and int(key) <= n:
            self.select(int(key) - 1)
        elif key == "enter" and n:
            self.focus, self.dirty = not self.focus, True
            self.zoom, self.center = 1.0, (0.5, 0.5)
        elif key in ("esc", "backspace"):
            self.focus, self.dirty = False, True
        elif key == " " and sel:
            self.toggle_play(sel)
        elif key == "r" and sel:
            self.replay(sel)
        elif key == "o" and sel:
            self.open_external(sel)
        elif key == "x" and sel:
            self.remove(self.selected)
        elif key == "C":
            self.clear()

    def handle_mouse(self, button: int, _col: int, row: int, pressed: bool) -> None:
        if button == 64:
            self.scroll(-3)
        elif button == 65:
            self.scroll(3)
        elif button == 0 and pressed and not self.focus and row < self.view:
            ln = self.top + row
            if ln < len(self.lines):
                i = self.lines[ln].item
                now = time.monotonic()
                if self.last_click[1] == i and now - self.last_click[0] < DOUBLE_CLICK:
                    self.focus = True
                self.last_click = (now, i)
                self.select(i, scroll=False)
        elif button == 0 and pressed and self.focus:
            self.focus, self.dirty = False, True

    def scroll(self, delta: int) -> None:
        if self.focus or not self.lines:
            return
        self.top = max(0, min(self.top + delta, len(self.lines) - self.view))
        self.follow = self.top >= len(self.lines) - self.view and delta > 0
        if self.selected >= 0:
            start, end = self.span(self.selected)
            if end <= self.top or start >= self.top + self.view:
                self.selected = self.lines[self.top].item
        self.scroll_pending = True

    def select(self, i: int, scroll: bool = True) -> None:
        if i != self.selected:
            self.zoom, self.center = 1.0, (0.5, 0.5)
        self.selected = i
        self.follow = i == len(self.items) - 1 and self.follow
        if scroll and self.starts:
            self.reveal(i)
        self.dirty = True

    def open_external(self, it: Item) -> None:
        if term.over_ssh():
            self.say("can't open: this pane runs on a remote host")
            return
        # Only media: the pusher chose the extension, and `open` would run anything it's
        # associated with (an .html, an .app). Otherwise open our own PNG of it.
        path = it.media.path if it.media.path.suffix.lower() in MEDIA_EXTS else it.media.still
        if path is None:
            self.say("can't open this one")
            return
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        if not shutil.which(opener):
            self.say(f"can't open: no {opener}")
            return
        subprocess.Popen(
            [opener, str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.say(f"opened {it.name}")

    def run_key(self, key: plugins.Key) -> None:
        pane = self

        class Ctx:
            @property
            def selected(self) -> plugins.ItemInfo | None:
                ok = 0 <= pane.selected < len(pane.items)
                return pane.items[pane.selected].info() if ok else None

            @property
            def items(self) -> list[plugins.ItemInfo]:
                return [it.info() for it in pane.items]

            def say(self, text: str) -> None:
                pane.say(clean(text, 200))

        try:
            key.fn(Ctx())
        except Exception as e:
            self.say(f"plugin {key.plugin}: {clean(e, 80)}")
