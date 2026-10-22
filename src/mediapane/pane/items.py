"""Items and series: adding, stepping, evicting, forgetting."""

from __future__ import annotations

import contextlib
import math
import shutil
import time
from pathlib import Path

from ..kitty import make_id
from ..layout import clean
from ..media import Media
from .common import ID_SPACE, MAX_ITEMS
from .model import Item, Step


class ItemsMixin:
    def decoded(self) -> None:
        """Add finished decodes, oldest push first."""
        while self.decoding and self.decoding[0][2].done():
            c, header, future, workdir, path = self.decoding.pop(0)
            name = clean(header.get("name") or "media")
            if dup := self.duplicate(header):  # an identical push was decoding alongside
                path.unlink(missing_ok=True)
                shutil.rmtree(workdir, ignore_errors=True)
                self.fold(dup)
                if c:
                    self.reply(c, ok=True, id=dup.id, duplicate=True)
                continue
            try:
                item = self.add(future.result(), header, workdir)
            except Exception as e:  # a bad file must not take the pane down
                path.unlink(missing_ok=True)
                shutil.rmtree(workdir, ignore_errors=True)
                if c:
                    self.reply(c, ok=False, error=f"{name}: {e}")
                continue
            if c:
                self.reply(c, ok=True, id=item.id)

    def add(self, media: Media, header: dict, workdir: Path) -> Item:
        self.make_room()
        name = clean(header.get("name") or media.path.name)
        host = clean(header.get("host", ""))
        tag = clean(header.get("tag") or "", 100)
        now = _when(header.get("added"))  # set when resuming
        if tag:  # a series: one entry per tag, stepped through
            it = next((x for x in self.items if x.tag == tag), None)
            step = header.get("step")
            label = clean(step, 40) if step is not None else str(len(it.steps) + 1 if it else 1)
            st = Step(media, workdir, label, host, now)
            if it is None:
                it = Item(self.new_id(), tag, host, media, workdir, now, tag=tag, steps=[st])
                self.items.append(it)
            else:
                at_newest = it.cur == len(it.steps) - 1
                it.steps.append(st)
                it.host, it.added = host, now
                if at_newest and not it.lapse:  # follow new steps unless you've stepped back
                    self.show_step(it, len(it.steps) - 1)
            self.say(f"{tag}: step {label}")
        else:
            it = Item(self.new_id(), name, host, media, workdir, now)
            it.digest = str(header.get("digest") or "")
            self.items.append(it)
            self.say(f"received {name}")
        self.updated = it.id
        if self.follow or self.selected < 0:
            if self.selected != self.items.index(it):
                self.zoom, self.center = 1.0, (0.5, 0.5)
            self.selected = self.items.index(it)
        self.relayout = self.dirty = True
        self.run_hooks(it)
        return it

    def fold(self, dup: Item) -> None:
        """The same file arrived again (a hook and an agent both showing it, say): treat it
        like a new arrival of the item that's already here, without adding a copy."""
        self.updated = dup.id
        if self.follow or self.selected < 0:
            self.selected = self.items.index(dup)
        self.relayout = self.dirty = True  # layout() reveals it while following
        self.say(f"already here: {dup.name}")

    def duplicate(self, header: dict) -> Item | None:
        """An item already showing exactly this file under the same name (not series steps:
        an unchanged sample is still a step)."""
        if header.get("tag"):
            return None
        name = clean(header.get("name") or "")
        digest = header.get("digest")
        return next((it for it in self.items if not it.steps and it.digest == digest
                     and it.name == name), None)  # fmt: skip

    def run_hooks(self, it: Item) -> None:
        for name, fn in self.api.item_hooks if self.api else []:
            try:
                fn(it.info())
            except Exception as e:  # a plugin's bug isn't the pane's crash
                self.say(f"plugin {name}: {clean(e, 80)}")

    def make_room(self) -> None:
        """Keep at most MAX_ITEMS pieces of media, dropping the oldest (a series' oldest
        step before whole entries, so a long run keeps its recent history)."""
        while self.items and sum(max(1, len(x.steps)) for x in self.items) >= MAX_ITEMS:
            first = self.items[0]
            if len(first.steps) > 1:
                self.drop_step(first, 0)
            else:
                self.remove(0)

    def show_step(self, it: Item, k: int, quiet: bool = False) -> None:
        """Show step k of a series. `quiet`: the caller redraws (the time-lapse's light path)."""
        k = max(0, min(k, len(it.steps) - 1))
        st = it.steps[k]
        if it.media is st.media and it.cur == k:
            return
        self.stop_video(it)
        before = (it.media.width, it.media.height)
        it.cur, it.media, it.workdir = k, st.media, st.workdir
        it.frame, it.next_frame, it.position, it.last_png, it.broken = 0, 0.0, 0.0, None, ""
        it.playing, it.native, it.native_failed = True, False, False
        # The terminal holds the previous step (maybe a stored animation): send this one afresh
        self.sent.pop(it.id, None)
        self.native_jobs.pop(it.id, None)
        if (st.media.width, st.media.height) != before:
            self.relayout = self.dirty = True
        elif not quiet:
            self.dirty = True

    def drop_step(self, it: Item, k: int) -> None:
        st = it.steps.pop(k)
        self.forget_files(st.media, st.workdir)
        if it.cur > k:
            it.cur -= 1
        elif it.cur == k:
            it.cur = -1  # force show_step to switch
            self.show_step(it, min(k, len(it.steps) - 1))
        self.relayout = self.dirty = True

    def new_id(self) -> int:
        if self.free_ids:
            return self.free_ids.pop()
        if self.id_count >= ID_SPACE:
            raise RuntimeError("out of image ids")
        n = (self.id_base + self.id_count) % ID_SPACE
        self.id_count += 1
        return make_id(n)

    def remove(self, i: int) -> None:
        it = self.items.pop(i)
        self.forget(it)
        if self.selected > i or self.selected >= len(self.items):
            self.selected -= 1
        self.focus = self.focus and self.selected >= 0
        self.zoom, self.center = 1.0, (0.5, 0.5)
        self.relayout = self.dirty = True

    def clear(self) -> None:
        for it in self.items:
            self.forget(it)
        self.items.clear()
        self.selected, self.top, self.focus, self.follow = -1, 0, False, True
        self.zoom, self.center = 1.0, (0.5, 0.5)
        self.cleared = True
        self.relayout = self.dirty = True

    def forget(self, it: Item) -> None:
        """Release everything an item holds: its player, terminal image, files and id."""
        self.stop_video(it)
        self.sent.pop(it.id, None)
        self.write(self.gfx.delete(it.id))
        for st in it.steps or [Step(it.media, it.workdir, "", "", 0.0)]:
            self.forget_files(st.media, st.workdir)
        self.free_ids.append(it.id)

    def forget_files(self, media: Media, workdir: Path) -> None:
        shutil.rmtree(workdir, ignore_errors=True)
        with contextlib.suppress(OSError):
            if self.session and self.session in media.path.parents:
                media.path.unlink()

    def visible_items(self) -> list[Item]:
        by_id = {it.id: it for it in self.items}
        return [by_id[i] for i in self.shown if i in by_id]


def _when(added: object) -> float:
    """A resumed item's original time, if it's a sane one; otherwise now."""
    now = time.time()
    try:
        t = float(added)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return now
    return t if math.isfinite(t) and 0 < t <= now + 86400 else now
