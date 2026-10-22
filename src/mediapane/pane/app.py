"""The pane: a scrollable gallery that anything can push media to.

One process, one event loop (selectors): keys and mouse on stdin, pushes on a Unix socket, video
frames from ffmpeg pipes, timers for animations, and a wake pipe for signals and worker threads.
The behaviour lives in mixins, one module each; this class owns the state and the loop.
"""

from __future__ import annotations

import codecs
import concurrent.futures
import contextlib
import os
import random
import re
import select
import selectors
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from collections import OrderedDict
from pathlib import Path

from .. import ipc, plugins, term
from ..kitty import Graphics
from ..layout import Line, clean, parse_keys
from ..media import Media
from .common import ESC_WAIT, ID_SPACE, MESSAGE_SECS, SCREEN_OFF, SCREEN_ON, SYNC_OFF, SYNC_ON
from .draw import DrawMixin
from .history import HistoryMixin
from .input import InputMixin
from .items import ItemsMixin
from .model import Conn, Item
from .play import PlaybackMixin
from .server import SocketMixin
from .video import FrameSink, _shm_path, _shm_unlink, _shm_write, _tiny_png

_REPLIES = re.compile(rb"\x1b_G[^\x1b]*\x1b\\|\x1b\[\?[\d;]*c")
_REPLY_END = re.compile(rb"\x1b\[\?[\d;]*c")


class Pane(InputMixin, PlaybackMixin, DrawMixin, ItemsMixin, HistoryMixin, SocketMixin):
    def __init__(
        self, sock_path: Path, fd_in: int = 0, fd_out: int = 1, resume: bool = False
    ) -> None:
        self.sock_path = sock_path
        self.resume = resume
        self.fd_in, self.fd_out = fd_in, fd_out
        self.tmux = term.in_tmux()
        self.gfx = Graphics(direct=term.direct_transfer(), tmux=self.tmux)
        self.ffmpeg = shutil.which("ffmpeg")
        self.hostname = clean(socket.gethostname())
        self.items: list[Item] = []
        # Image ids start at a random point, so two panes (or other graphics programs) sharing
        # a terminal window are unlikely to collide; ids of removed items are reused
        self.id_base, self.id_count, self.free_ids = random.randrange(ID_SPACE), 0, []
        self.top = 0  # first visible gallery line
        self.selected = -1
        self.follow = True  # keep the newest item in view
        self.focus = False  # one item, large
        self.help = False
        self.pasting = False
        self.zoom, self.center = 1.0, (0.5, 0.5)  # large view: magnification, centre (fraction)
        self.native_anim = False  # terminal plays animations itself (kitty a=f), over SSH only
        self.updated: int | None = None  # id of the item that changed last, for following
        self.api: plugins.API | None = None
        self.message, self.message_until = "", 0.0
        self.last_click = (0.0, -1)
        self.lines: list[Line] = []
        self.starts: list[int] = []
        self.shown: dict[int, tuple[int, int]] = {}  # item id -> cells it's displayed at
        self.sent: dict[int, tuple[tuple[int, int], object]] = {}  # what the terminal has
        self.rows, self.cols, self.cw, self.ch = 24, 80, *term.FALLBACK_CELL
        self.dirty = self.relayout = True
        self.scroll_pending = self.status_dirty = False
        self.drawn_top: int | None = None  # gallery top as last fully drawn, for scrolling
        self.running = True
        self.sel = selectors.DefaultSelector()
        self.server: socket.socket | None = None
        self.conns: list[Conn] = []
        self.received = 0
        self.session: Path | None = None
        self.sink: FrameSink | None = None
        self.inbuf, self.esc_at = "", 0.0
        self.early_input = b""  # keys that arrived while asking the terminal things at start
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        # Work off the event loop, in arrival order: decoding pushes (GIF frames, posters) and,
        # over SSH, scaling PNGs to the displayed size
        self.pool = concurrent.futures.ThreadPoolExecutor(2, thread_name_prefix="mediapane")
        # (connection, or None when resuming; header; decode; workdir; received file)
        self.decoding: list[tuple[Conn | None, dict, concurrent.futures.Future[Media], Path,
                                  Path]] = []  # fmt: skip
        self.scaled: OrderedDict[tuple, bytes] = OrderedDict()
        self.scaled_bytes = 0
        self.scaling: dict[tuple, concurrent.futures.Future[bytes]] = {}
        # item id -> (cells, media, all frames being scaled for storing in the terminal)
        self.native_jobs: dict[int, tuple[tuple[int, int], Media, concurrent.futures.Future]] = {}
        self.cleared = False  # emptied on purpose: don't keep the old history
        self.wake_r, self.wake_w = os.pipe()
        os.set_blocking(self.wake_w, False)

    def run(self) -> None:
        self.listen()
        self.session = _new_session()
        for sig in (signal.SIGWINCH, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, self.on_signal)
        with term.cbreak(self.fd_in):
            # Inside tmux the terminal's reply may not reach us, so don't ask: the gallery needs
            # kitty or Ghostty anyway, and both take shared memory
            shm = not self.gfx.direct and (self.tmux or self.query_shm())
            self.sink = FrameSink(self.gfx, self.session, shm)
            # Over SSH, a terminal that animates by itself saves re-sending every frame forever
            self.native_anim = self.gfx.direct and not self.tmux and self.query_animation()
            try:
                self.api = plugins.load()
            except Exception as e:  # plugins are extras: never fatal
                self.say(f"plugins failed: {e}")
            if self.api and self.api.errors:
                self.say(f"plugin {self.api.errors[0][0]} failed to load (mediapane plugins)")
            if self.resume:
                self.restore()
            self.sel.register(self.fd_in, selectors.EVENT_READ, self.on_input)
            self.sel.register(self.wake_r, selectors.EVENT_READ, self.on_wake)
            self.write(f"{SCREEN_ON}\x1b]2;mediapane {os.getpid()}\x07")
            keys, self.inbuf = parse_keys(self.decoder.decode(self.early_input))
            for key in keys:
                self.handle(key)
            try:
                while self.running:
                    if self.dirty:
                        self.render()
                    elif self.scroll_pending:
                        self.render_scroll()
                    elif self.status_dirty:
                        self.write(SYNC_ON + self.status_bar() + SYNC_OFF)
                        self.status_dirty = False
                    now = time.monotonic()
                    if frames := self.tick(now):
                        self.write(frames)
                    for key, _ in self.sel.select(self.timeout(now)):
                        key.data(key.fileobj)
                    now = time.monotonic()
                    if self.inbuf == "\x1b" and now - self.esc_at > ESC_WAIT:
                        self.inbuf = ""
                        self.handle("esc")
                    if self.message and now > self.message_until:
                        self.message, self.status_dirty = "", True
            except KeyboardInterrupt:
                pass
            finally:
                self.shutdown()

    def query_shm(self) -> bool:
        """Whether the terminal takes images from POSIX shared memory."""
        name = f"mp{os.getpid()}-probe"
        png = _tiny_png()
        try:
            _shm_write(name, png)
        except OSError:
            return False
        query = self.gfx.place_shm(31, 1, 1, _shm_path(name), len(png))
        try:
            return b"i=31;OK" in self.ask(query.replace("a=T,U=1,q=2", "a=q"))
        finally:
            _shm_unlink(name)  # in case the terminal didn't

    def query_animation(self) -> bool:
        """Whether the terminal stores animation frames (kitty a=f) and plays them itself.
        Terminals without it ignore the frame silently, so only an explicit OK counts."""
        pixel = "AAAA"  # one black RGB pixel
        reply = self.ask(
            f"\x1b_Ga=t,i=31,f=24,s=1,v=1,q=2;{pixel}\x1b\\"
            f"\x1b_Ga=f,i=31,f=24,s=1,v=1,z=100;{pixel}\x1b\\"
        )
        with contextlib.suppress(OSError):
            self.write(self.gfx.delete(31))
        return b"i=31;OK" in reply

    def ask(self, query: str) -> bytes:
        """Send a graphics query plus a device-attributes request (which every terminal
        answers, so silence can be told from slowness); return what came back in 0.5 s."""
        reply = b""
        os.write(self.fd_out, (query + "\x1b[c").encode())
        deadline = time.monotonic() + 0.5
        while not _REPLY_END.search(reply):
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([self.fd_in], [], [], left)[0]:
                break
            reply += os.read(self.fd_in, 1024)
        # Anything else that arrived is keys typed meanwhile: keep them for the event loop
        self.early_input += _REPLIES.sub(b"", reply)
        return reply

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
        for it in self.items:
            self.stop_video(it)
        for c in list(self.conns):
            self.drop(c)
        if self.sink:
            self.sink.close()
        with contextlib.suppress(OSError):
            # Delete our images one by one: "delete all" would also hit other tmux panes' images
            self.write("".join(self.gfx.delete(it.id) for it in self.items) + SCREEN_OFF)
        if self.server:
            self.server.close()
            with contextlib.suppress(OSError):
                self.sock_path.unlink()
        if self.session:
            with contextlib.suppress(Exception):  # history is a nicety: never block an exit
                self.save_history()
            shutil.rmtree(self.session, ignore_errors=True)
        if os.environ.get("MEDIAPANE_CLOSE_GHOSTTY"):
            # Mark our split by working directory (titles can be pinned) for the closer to find
            marker = f"/tmp/mediapane-closing-{os.getpid()}"
            with contextlib.suppress(OSError):
                self.write(f"\x1b]7;file://{self.hostname}{marker}\x07")
            _close_ghostty_split(marker)

    def on_signal(self, signum: int, _frame: object) -> None:
        if signum != signal.SIGWINCH:
            self.running = False
        self.wake(b"w")

    def wake(self, why: bytes) -> None:
        """Interrupt select() from a signal handler or worker thread."""
        with contextlib.suppress(OSError):
            os.write(self.wake_w, why)

    def on_wake(self, _fd: object) -> None:
        try:
            why = os.read(self.wake_r, 1024)
        except OSError:
            why = b""
        if b"w" in why:  # SIGWINCH: resized, or (in tmux) maybe reattached from a new terminal
            self.relayout = self.dirty = True
            if self.tmux:
                self.sent.clear()
        self.decoded()
        self.collect_scaled()

    def write(self, data: str) -> None:
        buf = data.encode("utf-8", "replace")
        while buf:
            n = os.write(self.fd_out, buf)
            buf = buf[n:]

    def say(self, text: str) -> None:
        self.message, self.message_until = text, time.monotonic() + MESSAGE_SECS
        self.status_dirty = True

    def timeout(self, now: float) -> float | None:
        deadlines = [it.next_frame for it in self.visible_items()
                     if it.media.kind == "anim" and it.playing and it.next_frame
                     and not it.waiting and not self.wants_native(it)]  # fmt: skip
        deadlines += [it.next_lapse for it in self.visible_items() if it.lapse and it.next_lapse]
        if self.message:
            deadlines.append(self.message_until)
        if self.inbuf == "\x1b":
            deadlines.append(self.esc_at + ESC_WAIT)
        return max(0.0, min(deadlines) - now) if deadlines else None


def _new_session() -> Path:
    """A private directory for this pane's files, after removing ones left by panes on this host
    that died without cleaning up (SIGKILL, power loss). Names carry the host: on a cluster the
    cache dir is often on a home shared by many nodes, whose pids mean nothing here."""
    root = ipc.cache_dir()
    root.mkdir(parents=True, exist_ok=True)
    host = ipc.host_tag()
    for old in root.glob(f"session-*@{host}-*"):
        m = re.fullmatch(rf"session-(\d+)@{re.escape(host)}-[^-]+", old.name)
        if m and not _alive(int(m[1])):
            shutil.rmtree(old, ignore_errors=True)
    return Path(tempfile.mkdtemp(prefix=f"session-{os.getpid()}@{host}-", dir=root))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _close_ghostty_split(marker: str) -> None:
    """Ghostty keeps a split it created with a command open after the command exits ("Process
    exited"), whatever wait-after-command says; close ours from a helper once we're gone. The
    helper gets its own session, so the pty hangup on our exit doesn't kill it."""
    script = (
        'tell application "Ghostty" to close item 1 of '
        f'(every terminal whose working directory is "{marker}")'
    )
    subprocess.Popen(
        ["sh", "-c", 'sleep 0.3; osascript -e "$0"', script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
