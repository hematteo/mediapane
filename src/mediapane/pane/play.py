"""Playback: animations, videos, time-lapse, zoom and pan."""

from __future__ import annotations

import contextlib
import os
import selectors

from ..layout import clean
from .common import LAPSE_FPS, MAX_ZOOM
from .model import Item
from .video import VideoPlayer


class PlaybackMixin:
    def tick(self, now: float) -> str:
        out = []
        for it in self.visible_items():
            if it.lapse:
                if it.next_lapse == 0.0:
                    it.next_lapse = now + 1 / LAPSE_FPS
                elif now >= it.next_lapse:
                    it.next_lapse = max(it.next_lapse + 1 / LAPSE_FPS, now)
                    self.show_step(it, (it.cur + 1) % len(it.steps), quiet=True)
                    if not self.dirty:  # same size: swap the image, rewrite one caption
                        cells = self.shown[it.id]
                        zoomed = self.focus and self.zoom > 1 and it.media.kind == "image"
                        img = self.upload_zoomed(it, cells) if zoomed else self.upload(it, cells)
                        out.append(img + self.caption_line(it))
                continue
            if it.media.kind != "anim" or not it.playing or self.wants_native(it):
                continue
            cells = self.shown[it.id]
            frames = it.media.frames
            if it.next_frame == 0.0:  # just became visible
                it.next_frame = now + frames[it.frame][1]
            elif now >= it.next_frame:
                nxt = (it.frame + 1) % len(frames)
                if self.gfx.direct:
                    # Over SSH frames are scaled on a worker: ask a few ahead, wait if not ready
                    px = round(cells[0] * self.cw)
                    for k in range(1, 5):
                        self.scaled_for(frames[(it.frame + k) % len(frames)][0], px)
                    if (frames[nxt][0], px) not in self.scaled:
                        it.waiting = True  # out of timeout() until a worker's wake
                        continue
                it.frame = nxt
                # keep cadence, but don't race to catch up after a stall
                it.next_frame = max(it.next_frame + frames[nxt][1], now)
                out.append(self.upload(it, cells))
        return "".join(out)

    def sync_videos(self) -> None:
        """Play exactly the videos that are visible and not paused; restart at a new width."""
        for it in self.items:
            if it.media.kind == "anim" and it.id not in self.shown:
                it.next_frame = 0.0
            if it.media.kind != "video":
                continue
            cells = self.shown.get(it.id)
            if cells is None:
                it.last_png = None  # an uncompressed frame can be MBs: shown again, it's the poster
            want = bool(cells and it.playing and self.ffmpeg and not it.broken)
            px = self.video_width(it, cells) if cells else 0
            if it.video and (not want or it.video.width != px):
                self.stop_video(it)
            if want and not it.video:
                self.start_video(it, px)

    def video_width(self, it: Item, cells: tuple[int, int]) -> int:
        w = min(round(cells[0] * self.cw), it.media.width or 10**6)
        return max(2, w - w % 2)

    def start_video(self, it: Item, px: int) -> None:
        assert self.ffmpeg and self.session
        direct = self.gfx.direct  # over SSH: fewer, compressed frames
        # Locally, uncompressed PNG: compressing cost ~18% of a core vs ~5%
        it.video = VideoPlayer(self.ffmpeg, it.media.path, it.position, px,
                               15 if direct else 24, 1 if direct else 0,
                               self.session / f"ffmpeg-{it.id}.log")  # fmt: skip
        self.sel.register(it.video.fd, selectors.EVENT_READ, lambda _fd: self.on_video(it))

    def stop_video(self, it: Item, finished: bool = False) -> None:
        vp = it.video
        if not vp:
            return
        with contextlib.suppress(KeyError, ValueError):
            self.sel.unregister(vp.fd)
        pos = vp.stop()
        it.video = None
        if finished:
            it.position, it.playing = 0.0, False
            if not vp.frames and (err := vp.error()):
                it.broken = f"can't play: {clean(err, 80)}"
            self.dirty = True
        else:
            it.position = pos

    def on_video(self, it: Item) -> None:
        vp = it.video
        if vp is None:
            return
        try:
            data = os.read(vp.fd, 1 << 20)
        except BlockingIOError:
            return
        if not data:
            self.stop_video(it, finished=True)
            return
        frames = vp.stream.feed(data)
        if not frames:
            return
        vp.frames += len(frames)
        it.last_png = frames[-1]  # if several arrived at once, only the newest matters
        cells = self.shown.get(it.id)
        if cells is None:
            return
        self.sent[it.id] = (cells, ("frame", id(it.last_png)))
        self.write(self.video_frame(it, cells, it.last_png))

    def video_frame(self, it: Item, cells: tuple[int, int], png: bytes) -> str:
        if self.gfx.direct or self.sink is None:
            return self.gfx.place(it.id, *cells, png=png)
        return self.sink.send(it.id, cells, png)

    def toggle_play(self, it: Item) -> None:
        if len(it.steps) > 1 and it.media.kind not in ("anim", "video"):
            self.toggle_lapse(it)
            return
        if it.media.kind not in ("anim", "video"):
            return
        it.playing = not it.playing
        it.next_frame = 0.0
        if it.native:
            self.write(self.gfx.animate(it.id, s=3 if it.playing else 1))
        self.dirty = True

    def toggle_lapse(self, it: Item) -> None:
        if len(it.steps) < 2:
            return
        it.lapse, it.next_lapse = not it.lapse, 0.0
        self.dirty = True

    def step(self, it: Item, k: int) -> None:
        it.lapse = False
        self.show_step(it, k)  # zoom stays: compare one region across steps
        if it.media.kind != "image":
            self.zoom, self.center = 1.0, (0.5, 0.5)
        self.dirty = True

    def zoom_by(self, factor: float) -> None:
        it = self.items[self.selected]
        if it.media.kind != "image" or it.media.still is None:
            self.say("zoom works on still images")
            return
        self.zoom = min(MAX_ZOOM, max(1.0, round(self.zoom * factor, 3)))
        if self.zoom == 1.0:
            self.center = (0.5, 0.5)
        self.pan(0, 0)

    def pan(self, dx: float, dy: float) -> None:
        """Move the zoomed view by a fraction of what's visible, staying inside the image."""
        half = 0.5 / self.zoom
        cx = min(1 - half, max(half, self.center[0] + dx / self.zoom))
        cy = min(1 - half, max(half, self.center[1] + dy / self.zoom))
        self.center = (round(cx, 4), round(cy, 4))
        self.dirty = True

    def replay(self, it: Item) -> None:
        if it.media.kind == "anim":
            it.frame, it.next_frame, it.playing = 0, 0.0, True
            if it.native:
                self.write(self.gfx.animate(it.id, c=1) + self.gfx.animate(it.id, s=3))
        elif it.media.kind == "video":
            self.stop_video(it)
            it.position, it.playing, it.broken = 0.0, True, ""
        self.dirty = True
