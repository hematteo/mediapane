"""The socket side: accepting pushes, streaming them to disk, answering."""

from __future__ import annotations

import contextlib
import json
import os
import re
import selectors
import socket
import stat

from .. import ipc
from ..layout import clean
from ..media import probe
from .common import MAX_PAYLOAD
from .model import Conn

PUSH_FIELDS = ("op", "name", "host", "tag", "step")


def _safe_name(name: object) -> str:
    base = os.path.basename(clean(name or "media"))
    base = re.sub(r"[^\w.\-]+", "_", base)[:100].lstrip(".")
    return base or "media"


class SocketMixin:
    def listen(self) -> None:
        path = self.sock_path
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            st = path.lstat()
        except FileNotFoundError:
            st = None
        if st is not None:
            if not stat.S_ISSOCK(st.st_mode):
                raise SystemExit(f"mediapane: {path} exists and isn't a socket; not touching it")
            if ipc.request("ping", path=path, timeout=1.0):
                raise SystemExit(f"mediapane: a pane is already running on {path}")
            path.unlink()  # stale, from a pane that didn't exit cleanly
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)  # socket readable and writable by us only
        try:
            self.server.bind(str(path))
        except OSError as e:
            raise SystemExit(
                f"mediapane: can't listen on {path}: {e} (set MEDIAPANE_SOCKET to a shorter path)"
            ) from e
        finally:
            os.umask(old)
        self.server.listen(16)
        self.server.setblocking(False)
        self.sel.register(self.server, selectors.EVENT_READ, self.on_accept)

    def on_accept(self, server: socket.socket) -> None:
        try:
            s, _ = server.accept()
        except OSError:
            return
        s.setblocking(False)
        conn = Conn(s)
        self.conns.append(conn)
        self.sel.register(s, selectors.EVENT_READ, lambda _s: self.on_conn(conn))

    def on_conn(self, c: Conn) -> None:
        try:
            data = c.sock.recv(1 << 20)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:  # client gone (mid-transfer, or after a payload-less request)
            self.drop(c)
            return
        if c.header is None:
            c.buf += data
            nl = c.buf.find(b"\n")
            if nl < 0:
                if len(c.buf) > ipc.MAX_HEADER:
                    self.reply(c, ok=False, error="header too long")
                return
            try:
                header = json.loads(c.buf[:nl])
                size = int(header.get("size", 0))
            except Exception:  # anything malformed, e.g. "size": null or 1e400
                self.reply(c, ok=False, error="bad header")
                return
            if not 0 <= size <= MAX_PAYLOAD:
                self.reply(c, ok=False, error="payload too large")
                return
            c.header = header
            data, c.remaining = bytes(c.buf[nl + 1 :]), size
            c.buf.clear()
            if header.get("op") == "push" and self.session:
                self.received += 1  # concurrent pushes each get their own file
                c.seq = self.received
                c.path = self.session / "in" / f"{c.seq:05d}-{_safe_name(header.get('name'))}"
                try:
                    c.path.parent.mkdir(exist_ok=True)
                    c.file = open(c.path, "wb")  # noqa: SIM115 - closed in finish()/drop()
                except OSError as e:
                    self.reply(c, ok=False, error=f"can't store it: {e.strerror}")
                    return
        chunk = data[: c.remaining]
        if c.file:
            try:
                c.file.write(chunk)
                c.digest.update(chunk)
            except OSError as e:  # disk full, say
                self.reply(c, ok=False, error=f"can't store it: {e.strerror}")
                return
        c.remaining -= len(chunk)
        if c.remaining <= 0:
            self.finish(c)

    def finish(self, c: Conn) -> None:
        header = c.header or {}
        op = header.get("op")
        if op == "push" and c.file and c.path and self.session:
            c.file.close()
            # Only the documented fields from a pusher; the digest is ours, whatever it says
            header = {k: header[k] for k in PUSH_FIELDS if k in header}
            header["digest"] = c.digest.hexdigest()
            if dup := self.duplicate(header):
                c.path.unlink(missing_ok=True)
                self.fold(dup)
                self.reply(c, ok=True, id=dup.id, duplicate=True)
                return
            with contextlib.suppress(KeyError, ValueError):
                self.sel.unregister(c.sock)  # nothing more to read; the reply comes later
            workdir = self.session / f"m{c.seq:05d}"
            future = self.pool.submit(probe, c.path, workdir)
            future.add_done_callback(lambda _f: self.wake(b"d"))
            self.decoding.append((c, header, future, workdir, c.path))
            if not future.done():
                self.say(f"decoding {clean(header.get('name') or 'media')}…")
        elif op == "ping":
            self.reply(c, ok=True, items=len(self.items), pid=os.getpid())
        elif op == "clear":
            self.clear()
            self.reply(c, ok=True)
        else:
            self.reply(c, ok=False, error=f"unknown op {clean(op)!r}")

    def reply(self, c: Conn, **msg: object) -> None:
        try:
            c.sock.setblocking(True)
            c.sock.settimeout(2.0)
            c.sock.sendall(json.dumps(msg).encode() + b"\n")
        except OSError:
            pass
        self.drop(c)

    def drop(self, c: Conn) -> None:
        with contextlib.suppress(KeyError, ValueError):
            self.sel.unregister(c.sock)
        c.sock.close()
        if c.file and not c.file.closed:
            c.file.close()
            if c.path:
                c.path.unlink(missing_ok=True)
        if c in self.conns:
            self.conns.remove(c)
