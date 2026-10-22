"""End to end: the real pane in a pseudo-terminal, fed through its socket."""

from __future__ import annotations

import fcntl
import os
import re
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from pathlib import Path

import pytest

from mediapane import ipc
from mediapane.kitty import PLACEHOLDER


class PtyPane:
    def __init__(
        self,
        tmp: Path,
        transfer: str = "file",
        rows: int = 40,
        cols: int = 120,
        extra_env: dict[str, str] | None = None,
        args: list[str] | None = None,
    ):
        # Unix socket paths max out at ~104 bytes; pytest's tmp_path is longer
        self.sock = Path(tempfile.mkdtemp(prefix="mp")) / "s"
        self.cache = tmp / "cache"
        self.master, slave = os.openpty()
        self.resize(rows, cols, slave)
        env = {k: v for k, v in os.environ.items()
               if k not in ("TMUX", "SSH_TTY", "SSH_CONNECTION", "MEDIAPANE_SOCKET")}  # fmt: skip
        env |= {"MEDIAPANE_TRANSFER": transfer, "XDG_CACHE_HOME": str(self.cache)}
        env |= extra_env or {}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "mediapane", "pane", "--socket", str(self.sock), *(args or [])],
            stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True,
        )  # fmt: skip
        os.close(slave)
        self.out = bytearray()
        self.lock = threading.Lock()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.wait_for(lambda: self.sock.exists(), "socket")
        self.wait_for(lambda: "\x1b[?1049h" in self.text(), "pane drawn")  # keys work from here

    def resize(self, rows: int, cols: int, fd: int | None = None) -> None:
        packed = struct.pack("HHHH", rows, cols, cols * 10, rows * 20)  # 10 x 20 px cells
        fcntl.ioctl(self.master if fd is None else fd, termios.TIOCSWINSZ, packed)

    def _read(self) -> None:
        while True:
            try:
                data = os.read(self.master, 65536)
            except OSError:
                return
            if not data:
                return
            with self.lock:
                self.out += data

    def text(self) -> str:
        with self.lock:
            return self.out.decode("utf-8", "replace")

    def wait_for(self, cond, what: str, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cond():
                return
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}; output tail: {self.text()[-500:]!r}")

    def push(self, path: Path) -> dict:
        reply = ipc.request("push", path.read_bytes(), path=self.sock, name=path.name)
        assert reply is not None
        return reply

    def keys(self, s: str) -> None:
        os.write(self.master, s.encode())

    def close(self) -> int:
        if self.proc.poll() is None:
            self.keys("q")
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.reader.join(5)  # everything the pane wrote, read, before callers look at text()
        os.close(self.master)
        return self.proc.returncode


def placements(text: str, image_id: int) -> list[str]:
    return re.findall(rf"\x1b_G(a=T,U=1[^;]*,i={image_id},[^;]*);", text)


@pytest.fixture
def pane(tmp_path):
    p = PtyPane(tmp_path)
    yield p
    p.close()


def test_ping_and_empty_screen(pane):
    reply = ipc.request("ping", path=pane.sock)
    assert reply and reply["ok"] and reply["items"] == 0
    pane.wait_for(lambda: "waiting for media" in pane.text(), "empty screen")


def test_push_image_draws_placeholders(pane, png):
    reply = pane.push(png)
    assert reply["ok"]
    image_id = reply["id"]
    pane.wait_for(lambda: placements(pane.text(), image_id), "image upload")
    ctl = placements(pane.text(), image_id)[0]
    assert "t=f" in ctl
    c, r = map(int, re.search(r"c=(\d+),r=(\d+)", ctl).groups())
    # 300x200 px at 10x20 px cells, upscaled 2x at most: 60 x 20 cells
    assert (c, r) == (60, 20)
    pane.wait_for(lambda: pane.text().count(PLACEHOLDER) >= c * r, "placeholder cells")
    assert "plot.png" in pane.text() and "300×200" in pane.text()


def test_gif_animates_while_visible(pane, gif):
    image_id = pane.push(gif)["id"]
    pane.wait_for(lambda: len(placements(pane.text(), image_id)) >= 8, "animation frames")
    assert all("t=f" in c for c in placements(pane.text(), image_id))


def test_bad_payload_is_rejected_and_pane_survives(pane, tmp_path):
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"not media at all")
    reply = pane.push(junk)
    assert reply["ok"] is False and reply["error"].startswith("junk.bin: ")
    assert ipc.request("ping", path=pane.sock)["ok"]


def test_video_plays_and_finishes(pane, mp4):
    image_id = pane.push(mp4)["id"]
    # 2 s clip at 24 fps max: expect a stream of frame uploads, then it stops
    pane.wait_for(lambda: len(placements(pane.text(), image_id)) >= 20, "frames", timeout=15)
    pane.wait_for(lambda: "paused" in pane.text(), "video end", timeout=15)


def test_follow_selects_newest_and_keys_navigate(pane, png, gif):
    first = pane.push(png)["id"]
    pane.push(gif)
    pane.wait_for(lambda: "2 items" in pane.text(), "two items")
    pane.keys("g")  # select first
    time.sleep(0.3)
    pane.keys("x")  # remove it
    pane.wait_for(lambda: ipc.request("ping", path=pane.sock)["items"] == 1, "removal")
    # the first item's image was freed (the pty reader may lag the pane under load: wait)
    pane.wait_for(lambda: f"\x1b_Ga=d,d=I,i={first}," in pane.text(), "first image freed")
    pane.wait_for(lambda: "1 item" in pane.text().rsplit("\x1b[?2026h", 1)[-1], "redraw")


def test_resize_relayouts(pane, png):
    image_id = pane.push(png)["id"]
    pane.wait_for(lambda: placements(pane.text(), image_id), "image upload")
    pane.resize(20, 60)
    os.kill(pane.proc.pid, signal.SIGWINCH)
    pane.wait_for(
        lambda: any("c=29" in p or "r=16" in p for p in placements(pane.text(), image_id)),
        "re-upload at new size",
    )


def test_quit_cleans_up(tmp_path, png):
    p = PtyPane(tmp_path)
    image_id = p.push(png)["id"]
    p.wait_for(lambda: placements(p.text(), image_id), "upload")
    assert p.close() == 0
    assert not p.sock.exists()
    assert not list((p.cache / "mediapane").glob("session-*"))
    assert f"\x1b_Ga=d,d=I,i={image_id}," in p.text()  # our images freed, one by one
    assert "d=A" not in p.text()  # never "delete all": other tmux panes' images would go too


def test_untrusted_names_cannot_inject_escapes(pane, png):
    evil = "\x1b]0;PWNED\x07\x1b]52;c;aGk=\x07\x9b2J.png"
    reply = ipc.request("push", png.read_bytes(), path=pane.sock, name=evil, host="\x1b[31mx")
    assert reply["ok"]
    pane.wait_for(lambda: "PWNED" in pane.text(), "caption")
    out = pane.text()
    assert "\x1b]0;PWNED" not in out and "\x1b]52" not in out and "\x9b" not in out
    assert "\x1b[31mx" not in out


@pytest.mark.parametrize(
    "header",
    [b'{"op":"push","size":null}', b'{"op":"push","size":1e400}', b"[1,2]", b"\xff\xfe", b"{"],
)
def test_malformed_headers_are_rejected(pane, header):
    import socket as sk

    with sk.socket(sk.AF_UNIX, sk.SOCK_STREAM) as s:
        s.connect(str(pane.sock))
        s.sendall(header + b"\n")
        assert b'"ok": false' in s.recv(4096)
    assert ipc.request("ping", path=pane.sock)["ok"]


def test_odd_keys_and_split_sequences_dont_crash(pane, png):
    pane.push(png)
    pane.keys("²٣")  # Unicode digits that aren't 1-9
    pane.keys("\x1b[<65;1")  # a mouse report cut in half...
    time.sleep(0.1)
    pane.keys("0;5M")  # ...and its end
    pane.keys("\x1b[200~xCq\x1b[201~")  # pasted text isn't commands
    time.sleep(0.3)
    assert pane.proc.poll() is None
    assert ipc.request("ping", path=pane.sock)["items"] == 1


def distinct_pngs(tmp_path: Path, n: int) -> list[Path]:
    """n different 300x200 images (identical pushes fold into one item)."""
    from PIL import Image

    out = []
    for i in range(n):
        f = tmp_path / f"img{i}.png"
        Image.new("RGB", (300, 200), (40 * i % 256, 80, 160)).save(f)
        out.append(f)
    return out


def test_oldest_items_are_dropped_beyond_the_cap(tmp_path):
    p = PtyPane(tmp_path, extra_env={"MEDIAPANE_MAX_ITEMS": "3"})
    try:
        ids = [p.push(f)["id"] for f in distinct_pngs(tmp_path, 5)]
        assert ipc.request("ping", path=p.sock)["items"] == 3
        assert len(set(ids)) >= 3  # removed items' ids are reused, so not all 5 are distinct
        p.wait_for(lambda: f"\x1b_Ga=d,d=I,i={ids[0]}," in p.text(), "oldest freed")
    finally:
        p.close()


def test_direct_transfer(tmp_path, png):
    p = PtyPane(tmp_path, transfer="direct")
    try:
        p.push(png)
        p.wait_for(lambda: re.search(r"\x1b_Ga=T,U=1[^;]*t=d", p.text()), "inline upload")
    finally:
        p.close()


def test_decoding_does_not_block_and_keeps_order(pane, png, tmp_path):
    from PIL import Image

    big = tmp_path / "big.gif"
    frames = [Image.new("RGB", (480, 360), (i % 255, 80, 160)) for i in range(150)]
    frames[0].save(big, save_all=True, append_images=frames[1:], duration=40, loop=0)
    replies: dict[str, dict] = {}
    t = threading.Thread(target=lambda: replies.__setitem__("gif", pane.push(big)))
    t.start()
    time.sleep(0.1)
    started = time.monotonic()
    assert ipc.request("ping", path=pane.sock)["ok"]
    assert time.monotonic() - started < 0.5  # the pane answers while the GIF decodes
    replies["png"] = pane.push(png)
    t.join(30)
    assert replies["gif"]["ok"] and replies["png"]["ok"]
    assert replies["gif"]["id"] < replies["png"]["id"]  # pushed first, listed first


def test_status_bar_never_reaches_the_last_cell(tmp_path):
    from mediapane.layout import width
    from mediapane.pane import Pane

    pane = Pane(tmp_path / "unused.sock")
    for cols in (20, 40, 120):
        pane.rows, pane.cols = 10, cols
        for msg in ("", "x" * 200, "日本語のファイル名" * 10):
            pane.message = msg
            line = pane.status_bar().split("H", 1)[1].replace("\x1b[2K", "")
            assert width(line) <= cols - 1, (cols, msg[:10])
    pane.pool.shutdown()


def test_frame_sink_shared_memory_names_a_real_segment(tmp_path):
    import base64
    from multiprocessing import shared_memory

    from mediapane.kitty import Graphics
    from mediapane.pane import FrameSink

    sink = FrameSink(Graphics(direct=False, tmux=False), tmp_path, shm=True)
    seq = sink.send(7, (4, 2), b"\x89PNG fake frame")
    m = re.search(r"\x1b_G([^;]*);([^\x1b]*)\x1b\\", seq)
    assert m and "t=s" in m.group(1) and "S=15" in m.group(1)
    name = base64.b64decode(m.group(2)).decode()
    assert name.startswith("/mp")
    seg = shared_memory.SharedMemory(name=name.lstrip("/"))  # what the terminal would open
    try:
        assert bytes(seg.buf[:15]) == b"\x89PNG fake frame"
    finally:
        seg.close()
    sink.close()  # unlinks what the terminal didn't
    with pytest.raises(FileNotFoundError):
        shared_memory.SharedMemory(name=name.lstrip("/"))
    assert not list(tmp_path.iterdir())  # nothing went to disk


def test_png_stream_reassembles_frames_from_any_split(png):
    from mediapane.pane import PngStream

    one = png.read_bytes()
    stream = (one + one + one)[:-7]  # three frames, the last one incomplete
    for step in (1, 7, 4096):
        s = PngStream()
        got = []
        for i in range(0, len(stream), step):
            got += s.feed(stream[i : i + step])
        assert got == [one, one]
        assert s.feed(one[-7:]) == [one]


def test_scrolling_redraws_only_exposed_lines(pane, tmp_path):
    for f in distinct_pngs(tmp_path, 4):
        pane.push(f)  # four 20-row images: far taller than the 40-row pane
    pane.wait_for(lambda: "4 items" in pane.text(), "items")
    pane.keys("g")
    time.sleep(0.4)
    before = len(pane.out)
    pane.keys("j")
    time.sleep(0.4)
    step = pane.out[before:].decode("utf-8", "replace")
    assert "\x1b[1;39r\x1b[1S\x1b[r" in step  # the terminal shifts the screen itself
    assert "\x1b[2J" not in step  # no full redraw
    assert len(step.encode()) < 4000  # vs ~25 KB for redrawing 40 rows of placeholders


def _item(pane, media, **kw):
    from mediapane.pane import Item

    it = Item(pane.new_id(), "x", "", media, Path("/nonexistent"), 0.0, **kw)
    pane.items.append(it)
    return it


def test_no_busy_wait_while_a_frame_is_being_scaled(tmp_path, gif):
    from mediapane.kitty import Graphics
    from mediapane.media import probe
    from mediapane.pane import Pane

    pane = Pane(tmp_path / "unused.sock")
    pane.gfx = Graphics(direct=True, tmux=False)
    it = _item(pane, probe(gif, tmp_path / "w"), cells=(10, 5))
    pane.shown = {it.id: (10, 5)}
    pane.tick(100.0)  # becomes visible: schedules its next frame
    pane.tick(it.next_frame + 1)  # due, but that frame isn't scaled yet
    assert it.waiting
    assert pane.timeout(it.next_frame + 1) is None  # sleeps until a worker wakes us, no spin
    pane.pool.shutdown(wait=True)
    pane.collect_scaled()
    assert not it.waiting
    pane.pool = None


def test_off_screen_videos_drop_their_last_frame(tmp_path):
    from mediapane.media import Media
    from mediapane.pane import Pane

    pane = Pane(tmp_path / "unused.sock")
    it = _item(pane, Media(Path("/v.mp4"), "video", 64, 36, None), last_png=b"x" * 1000)
    pane.ffmpeg = None
    pane.shown = {}
    pane.sync_videos()
    assert it.last_png is None
    pane.pool.shutdown()


def test_session_cleanup_only_touches_this_hosts_dead_panes(tmp_path, monkeypatch):
    from mediapane import ipc
    from mediapane.pane import _new_session

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    root = tmp_path / "mediapane"
    root.mkdir()
    host = ipc.host_tag()
    other = root / "session-1@some-other-node-abcd1234"  # pid 1 is meaningless across hosts
    dead = root / f"session-999999@{host}-abcd1234"
    alive = root / f"session-{os.getpid()}@{host}-abcd1234"
    for d in (other, dead, alive):
        d.mkdir()
    new = _new_session()
    assert other.exists() and alive.exists() and not dead.exists()
    assert new.name.startswith(f"session-{os.getpid()}@{host}-")
