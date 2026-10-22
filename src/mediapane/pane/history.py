"""History: what `mediapane pane --resume` brings back."""

from __future__ import annotations

import json
import os
import shutil
import time

from .. import ipc
from ..layout import clean
from ..media import probe
from .common import HISTORY_MB
from .model import Step


class HistoryMixin:
    def save_history(self) -> None:
        """Keep what's in the pane for `mediapane pane --resume`: the received files and their
        headers, newest kept first within MEDIAPANE_HISTORY_MB."""
        hist = ipc.cache_dir() / f"history-{ipc.host_tag()}"
        if HISTORY_MB <= 0 or not self.session:
            return
        records = []
        for it in self.items:
            for st in it.steps or [Step(it.media, it.workdir, "", it.host, it.added)]:
                header: dict = {"name": st.media.path.name.split("-", 1)[-1], "host": st.host}
                if not it.steps:
                    header |= {"name": it.name, "digest": it.digest}
                else:
                    header |= {"tag": it.tag, "step": st.label}
                records.append((st.media.path, header, st.added))
        # Pushes (or resumed items) still decoding are part of the session too
        for _c, header, _future, _workdir, path in self.decoding:
            meta = {k: clean(header[k]) for k in ("name", "host", "tag", "step") if k in header}
            records.append((path, meta, time.time()))
        if not records:
            if self.cleared:  # emptied on purpose: forget the old history as well
                shutil.rmtree(hist, ignore_errors=True)
            return
        budget, kept = HISTORY_MB * 2**20, []
        for path, header, added in reversed(records):
            size = path.stat().st_size if path.exists() else 0
            if size and size <= budget:
                budget -= size
                kept.append((path, header, added))
        # Private (homes are often shared), and per pane while being written: two panes
        # quitting at once each finish their own copy; the last to finish is what's kept
        tmp = hist.with_name(f"{hist.name}.new-{os.getpid()}")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(mode=0o700, parents=True)
        (tmp / "in").mkdir(mode=0o700)
        with open(tmp / "items.jsonl", "w") as f:
            for n, (path, header, added) in enumerate(reversed(kept)):
                dest = tmp / "in" / f"{n:05d}-{path.name.split('-', 1)[-1]}"
                os.replace(path, dest)
                f.write(json.dumps({"file": dest.name, "added": added, **header}) + "\n")
        shutil.rmtree(hist, ignore_errors=True)
        os.replace(tmp, hist)

    def restore(self) -> None:
        """Bring back the last session's items (decoded like pushes, in order)."""
        hist = ipc.cache_dir() / f"history-{ipc.host_tag()}"
        try:
            lines = (hist / "items.jsonl").read_text().splitlines()
        except OSError:
            self.say("nothing to resume")
            return
        assert self.session
        for line in lines:
            try:
                rec = json.loads(line)
                src = hist / "in" / os.path.basename(rec["file"])
                self.received += 1
                dest = self.session / "in" / f"{self.received:05d}-{src.name.split('-', 1)[-1]}"
                dest.parent.mkdir(exist_ok=True)
                try:
                    os.link(src, dest)  # instant, and the history stays intact if we crash
                except OSError:
                    shutil.copy2(src, dest)
            except (OSError, ValueError, KeyError, TypeError):
                continue
            workdir = self.session / f"m{self.received:05d}"
            keys = ("name", "host", "tag", "step", "added", "digest")
            header = {k: rec[k] for k in keys if rec.get(k) is not None}
            future = self.pool.submit(probe, dest, workdir)
            future.add_done_callback(lambda _f: self.wake(b"d"))
            self.decoding.append((None, header, future, workdir, dest))
