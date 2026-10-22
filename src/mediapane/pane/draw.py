"""Layout and drawing: the gallery, the large view, captions, and image uploads."""

from __future__ import annotations

import time
from pathlib import Path

from .. import term
from ..kitty import placeholder_row
from ..layout import clip, fit, gallery_lines, width
from ..media import cropped_png, scaled_png
from .common import (
    ACCENT,
    BADGE,
    BADGE_SEL,
    BOLD,
    CORE_KEYS,
    DIM,
    HELP,
    NATIVE_MAX_FRAMES,
    NATIVE_MAX_PIXELS,
    OFF,
    SCALED_CACHE,
    SYNC_OFF,
    SYNC_ON,
)
from .model import Item
from .video import _scale_all


def _timeline(cur: int, n: int, cells: int = 16) -> str:
    """A small track with a dot at the current step."""
    if n < 2:
        return ""
    w = min(n, cells)
    pos = round(cur / (n - 1) * (w - 1))
    return f" {DIM}{'─' * pos}{OFF}{ACCENT}●{OFF}{DIM}{'─' * (w - 1 - pos)}{OFF}"


class DrawMixin:
    @property
    def view(self) -> int:
        return max(1, self.rows - 1)  # last row is the status bar

    def layout(self) -> None:
        self.rows, self.cols, _, _ = term.winsize(self.fd_out)
        self.cw, self.ch = term.cell_size(self.fd_out)
        for it in self.items:
            m = it.media
            it.cells = (
                fit(m.width, m.height, self.cols - 2, self.view - 3, self.cw, self.ch)
                if m.still and not it.broken
                else (0, 0)
            )
        self.lines, self.starts = gallery_lines([it.cells for it in self.items])
        if self.follow and self.items:
            ids = [it.id for it in self.items]
            self.selected = ids.index(self.updated) if self.updated in ids else len(ids) - 1
            self.reveal(self.selected)
        self.top = max(0, min(self.top, len(self.lines) - self.view))

    def span(self, i: int) -> tuple[int, int]:
        """First and one-past-last gallery line of item i."""
        end = self.starts[i + 1] if i + 1 < len(self.starts) else len(self.lines)
        return self.starts[i], end

    def fresh_layout(self) -> None:
        """Lay out now if items changed since: line maps must match the item list."""
        if self.relayout:
            self.relayout = False
            self.layout()

    def reveal(self, i: int) -> None:
        """Scroll so item i is in view (its top, if it's taller than the view)."""
        self.fresh_layout()
        if not 0 <= i < len(self.starts):
            return
        start, end = self.span(i)
        if start < self.top:
            self.top = start
        elif end > self.top + self.view:
            self.top = min(start, end - self.view)

    def render(self) -> None:
        if self.relayout:
            self.layout()
            self.relayout = False
        out = [SYNC_ON, "\x1b[H\x1b[2J"]
        self.shown = {}
        if self.help:
            out.append(self.draw_help())
        elif self.focus and 0 <= self.selected < len(self.items):
            out.append(self.draw_focus())
        elif self.items:
            out.append(self.draw_gallery())
        else:
            out.append(self.draw_empty())
        # Free what scrolled out of view: keeps the terminal's image memory to what's on screen,
        # and a terminal evicting images on its own would leave `sent` wrong
        out.append(self.evict())
        out.append(self.status_bar())
        out.append(SYNC_OFF)
        self.write("".join(out))
        gallery = not (self.help or self.focus) and bool(self.items)
        self.drawn_top = self.top if gallery else None
        self.dirty = self.scroll_pending = self.status_dirty = False
        self.sync_videos()

    def render_scroll(self) -> None:
        """After a scroll: let the terminal shift what's on screen (a scroll region above the
        status bar) and draw only the exposed lines, instead of the whole screen. Placeholder
        cells are text, so the images move with them. Over SSH this is most of the traffic."""
        delta = self.top - (self.drawn_top if self.drawn_top is not None else self.top)
        if self.drawn_top is None or abs(delta) >= self.view:
            self.render()
            return
        n = abs(delta)
        exposed = range(self.view - n, self.view) if delta > 0 else range(n)
        out = [SYNC_ON, self.visible_uploads()]
        if n:
            out.append(f"\x1b[1;{self.view}r\x1b[{n}{'S' if delta > 0 else 'T'}\x1b[r")
            out.extend(f"\x1b[{y + 1};1H\x1b[2K{self.gallery_line(y)}" for y in exposed)
        # Captions still on screen: the selection badge may have moved
        for y in range(self.view):
            ln = self.top + y
            if y not in exposed and ln < len(self.lines) and self.lines[ln].kind == "caption":
                out.append(f"\x1b[{y + 1};1H\x1b[2K{self.caption(self.lines[ln].item)}")
        out.append(self.evict())
        out.append(self.status_bar())
        out.append(SYNC_OFF)
        self.write("".join(out))
        self.drawn_top = self.top
        self.scroll_pending = self.status_dirty = False
        self.sync_videos()

    def evict(self) -> str:
        """Delete images that left the screen."""
        out = []
        for image_id in [k for k in self.sent if k not in self.shown]:
            del self.sent[image_id]
            out.append(self.gfx.delete(image_id))
            self.native_jobs.pop(image_id, None)
            for it in self.items:
                if it.id == image_id:
                    it.native = False
        return "".join(out)

    def visible_uploads(self) -> str:
        """Recompute what's on screen (`shown`) and upload whatever the terminal lacks."""
        self.shown = {}
        uploads = []
        last = min(len(self.lines), self.top + self.view)
        for i in sorted({self.lines[ln].item for ln in range(self.top, last)}):
            it = self.items[i]
            if it.cells[0]:
                self.shown[it.id] = it.cells
                uploads.append(self.upload(it, it.cells))
        return "".join(uploads)

    def gallery_line(self, y: int) -> str:
        """Screen row y of the gallery (cursor positioning included)."""
        ln = self.top + y
        if ln >= len(self.lines):
            return ""
        line = self.lines[ln]
        it = self.items[line.item]
        if line.kind == "caption":
            return f"\x1b[{y + 1};1H{self.caption(line.item)}"
        if line.kind == "image" and it.cells[0]:
            c = it.cells[0]
            return f"\x1b[{y + 1};{(self.cols - c) // 2 + 1}H" + placeholder_row(it.id, line.row, c)
        return ""

    def draw_gallery(self) -> str:
        return self.visible_uploads() + "".join(self.gallery_line(y) for y in range(self.view))

    def draw_focus(self) -> str:
        it = self.items[self.selected]
        m = it.media
        c, r = (
            fit(m.width, m.height, self.cols - 2, self.view - 2, self.cw, self.ch)
            if m.still and not it.broken
            else (0, 0)
        )
        out = [f"\x1b[1;1H{self.caption(self.selected)}"]
        if c:
            self.shown[it.id] = (c, r)
            zoomed = self.zoom > 1 and it.media.kind == "image"
            out.insert(0, self.upload_zoomed(it, (c, r)) if zoomed else self.upload(it, (c, r)))
            top, left = 2 + (self.view - 1 - r) // 2, (self.cols - c) // 2 + 1
            out.extend(f"\x1b[{top + k};{left}H" + placeholder_row(it.id, k, c) for k in range(r))
        return "".join(out)

    def draw_empty(self) -> str:
        lines = [
            f"{BOLD}mediapane{OFF} is waiting for media",
            "",
            f"{DIM}from a shell{OFF}    mediapane push plot.png run.mp4",
            f"{DIM}from Python{OFF}     import mediapane; mediapane.show(tensor)",
            f"{DIM}from a folder{OFF}   mediapane watch outputs/",
            f"{DIM}from a server{OFF}   mediapane ssh user@host, then push there",
        ]
        return self.centred(lines)

    def draw_help(self) -> str:
        keys = self.api.keys.values() if self.api else []
        extra = [(k.key, f"{k.help}  [{k.plugin}]") for k in keys if k.key not in CORE_KEYS]
        col = max(len(k) for k, _ in HELP + extra) + 3
        lines = [f"{BOLD}keys{OFF}", ""] + [f"{k:<{col}}{DIM}{v}{OFF}" for k, v in HELP + extra]
        return self.centred([*lines, "", f"{DIM}any key to close{OFF}"])

    def centred(self, lines: list[str]) -> str:
        top = max(1, (self.view - len(lines)) // 2)
        return "".join(f"\x1b[{top + k};3H{clip(t, self.cols - 4)}" for k, t in enumerate(lines))

    def caption(self, i: int) -> str:
        it = self.items[i]
        m = it.media
        badge = BADGE_SEL if i == self.selected else BADGE
        parts = [f" {badge} {i + 1} {OFF} {BOLD}{it.name}{OFF}"]
        if it.steps:
            n = len(it.steps)
            lapse = f"  {ACCENT}▶ time-lapse{OFF}" if it.lapse else ""
            parts.append(
                f"{ACCENT}step {it.steps[it.cur].label}{OFF} {DIM}{it.cur + 1}/{n}{OFF}"
                f"{_timeline(it.cur, n)}{lapse}"
            )
        if self.focus and self.zoom > 1 and i == self.selected:
            parts.append(f"{ACCENT}zoom {self.zoom:.1f}×{OFF} {DIM}arrows pan, 0 resets{OFF}")
        if m.width:
            parts.append(f"{DIM}{m.width}×{m.height}{OFF}")
        if m.label:
            paused = m.kind in ("anim", "video") and not it.playing
            parts.append(f"{ACCENT}{m.label}{' · paused' if paused else ''}{OFF}")
        if it.broken:
            parts.append(f"{DIM}({it.broken}){OFF}")
        elif not m.still:
            parts.append(f"{DIM}(no preview{': install ffmpeg' if m.kind == 'video' else ''}){OFF}")
        if it.host and it.host != self.hostname:
            parts.append(f"{DIM}from {it.host}{OFF}")
        added = it.steps[it.cur].added if it.steps else it.added
        parts.append(f"{DIM}{time.strftime('%H:%M:%S', time.localtime(added))}{OFF}")
        return clip("  ".join(parts), self.cols)

    def caption_line(self, it: Item) -> str:
        """Redraw just `it`'s caption, wherever it is on screen (the time-lapse's light path)."""
        i = self.items.index(it)
        if self.focus:
            return f"\x1b[1;1H\x1b[2K{self.caption(i)}" if i == self.selected else ""
        y = self.starts[i] + 1 - self.top if i < len(self.starts) else -1
        return f"\x1b[{y + 1};1H\x1b[2K{self.caption(i)}" if 0 <= y < self.view else ""

    def status_bar(self) -> str:
        n = len(self.items)
        left = self.message or f"mediapane · {n} item{'s' * (n != 1)}"
        if not self.message and n and self.follow:
            left += " · following"
        left = clip(left, max(0, self.cols - 2))
        hints = "j/k scroll  n/p item  ⏎ view  ␣ play  o open  ? help  q quit"
        room = self.cols - 2 - width(left)
        hint = hints if room > len(hints) else ("? help" if room > 6 else "")
        pad = " " * max(0, room - len(hint))
        # Never write the last cell of the last row: some terminals scroll the screen when you do
        return f"\x1b[{self.rows};1H\x1b[2K {BOLD}{left}{OFF}{pad}{DIM}{hint}{OFF}"

    def upload(self, it: Item, cells: tuple[int, int]) -> str:
        """Image data for `it` at `cells`, unless the terminal already has exactly that."""
        if it.media.kind == "video" and it.last_png is not None:
            key: object = ("frame", id(it.last_png))
            if self.sent.get(it.id) == (cells, key):
                return ""
            self.sent[it.id] = (cells, key)
            return self.video_frame(it, cells, it.last_png)
        if self.wants_native(it):
            if self.sent.get(it.id) == (cells, "native"):
                return ""  # stored in the terminal, playing or paused there
            if native := self.upload_native(it, cells):
                return native
            # frames still being prepared: show the current one meanwhile
        path = self.current_path(it)
        if path is None or self.sent.get(it.id) == (cells, path):
            return ""
        c, r = cells
        if self.gfx.direct:
            png = self.scaled_for(path, round(c * self.cw))
            if png is None:
                return ""  # being scaled on a worker; drawn when it's ready
            self.sent[it.id] = (cells, path)
            return self.gfx.place(it.id, c, r, png=png)
        self.sent[it.id] = (cells, path)
        return self.gfx.place(it.id, c, r, path=path)

    def wants_native(self, it: Item) -> bool:
        """Whether `it` is (or is about to be) played by the terminal itself."""
        return it.media.kind == "anim" and self.native_anim and not it.native_failed

    def upload_native(self, it: Item, cells: tuple[int, int]) -> str:
        """Store all of an animation's frames in the terminal once and let it loop them: over
        SSH that replaces a stream of frames with a single upload. The frames are scaled in
        one worker job and held only until sent (not in the LRU, which a long GIF would
        churn). "" while that runs; too big, and the item falls back to streaming."""
        frames = it.media.frames
        job = self.native_jobs.get(it.id)
        if job is None or job[0] != cells or job[1] is not it.media:
            px, py = round(cells[0] * self.cw), round(cells[1] * self.ch)
            if len(frames) > NATIVE_MAX_FRAMES or len(frames) * px * py > NATIVE_MAX_PIXELS:
                it.native_failed = True
                return ""
            future = self.pool.submit(_scale_all, [f for f, _ in frames], px)
            future.add_done_callback(lambda _f: self.wake(b"s"))
            self.native_jobs[it.id] = (cells, it.media, future)
            return ""
        if not job[2].done():
            return ""
        del self.native_jobs[it.id]
        try:
            pngs = job[2].result()
        except Exception:
            it.native_failed = True
            return ""
        c, r = cells
        out = [self.gfx.place(it.id, c, r, png=pngs[0])]
        for png, (_, gap) in zip(pngs[1:], frames[1:], strict=True):
            out.append(self.gfx.frame(it.id, png, round(gap * 1000)))
        out.append(self.gfx.animate(it.id, r=1, z=round(frames[0][1] * 1000)))
        out.append(self.gfx.animate(it.id, s=3 if it.playing else 1, v=1))  # loop forever
        self.sent[it.id] = (cells, "native")
        it.native = True
        return "".join(out)

    def upload_zoomed(self, it: Item, cells: tuple[int, int]) -> str:
        """Part of a still image, enlarged: cropped and scaled on a worker."""
        path = it.media.still
        if path is None:
            return ""
        c, r = cells
        key = ("zoom", path, round(c * self.cw), round(r * self.ch), self.zoom, self.center)
        if self.sent.get(it.id) == (cells, key):
            return ""
        png = self.scaled.get(key)
        if png is None:
            if key not in self.scaling:
                future = self.pool.submit(
                    cropped_png, path, key[2], key[3], self.zoom, *self.center
                )
                future.add_done_callback(lambda _f: self.wake(b"s"))
                self.scaling[key] = future
            return ""
        self.sent[it.id] = (cells, key)
        return self.gfx.place(it.id, c, r, png=png)

    def current_path(self, it: Item) -> Path | None:
        if it.media.kind == "anim":
            return it.media.frames[it.frame][0]
        return it.media.still

    def scaled_for(self, path: Path, px: int) -> bytes | None:
        """PNG of `path` scaled to `px` wide for inline transfer, or None while a worker makes
        it. Paths here are immutable (stills and animation frames), so they make safe keys."""
        key = (path, px)
        if key in self.scaled:
            self.scaled.move_to_end(key)
            return self.scaled[key]
        if key not in self.scaling:
            future = self.pool.submit(scaled_png, path, px)
            future.add_done_callback(lambda _f: self.wake(b"s"))
            self.scaling[key] = future
        return None

    def collect_scaled(self) -> None:
        if any(job[2].done() for job in self.native_jobs.values()):
            self.dirty = True
        for key, future in list(self.scaling.items()):
            if not future.done():
                continue
            del self.scaling[key]
            try:
                png = future.result()
            except Exception:
                for it in self.items:  # the file vanished or is corrupt: mark, don't crash
                    if key[0] in (it.media.still, *(f for f, _ in it.media.frames)):
                        it.broken, self.relayout = "can't display", True
                self.dirty = True
                continue
            self.scaled[key] = png
            self.scaled_bytes += len(png)
            for it in self.items:
                it.waiting = False
            while self.scaled_bytes > SCALED_CACHE and len(self.scaled) > 1:
                self.scaled_bytes -= len(self.scaled.popitem(last=False)[1])
            self.dirty = True
