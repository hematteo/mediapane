"""How media reaches the pane: a Unix socket, one connection per push.

Wire format, simple enough for `nc -U`: one JSON header line, then `size` bytes of payload.
    {"op": "push", "name": "fig.png", "size": 1234, "host": "gpu03"}\\n<1234 bytes>
Optional header fields: "tag" and "step" put the item in a series (one gallery entry, stepped
through with arrow keys). The pane answers with one JSON line, e.g. {"ok": true}. Other ops:
"ping", "clear".

When no pane is reachable and $MEDIAPANE_SPOOL names a directory (say, on a home shared by a
cluster's nodes), pushes are written there instead, one file per push in the same wire format
(`<time_ns>-<host>-<pid>.mp`, written as `.tmp` then renamed), for `mediapane relay` (from the
mediapane-relay plugin) to forward from a machine that can reach the pane.

Remote hosts reach a pane through `mediapane ssh`, which forwards a socket at
/tmp/mediapane-<user>-<random>.sock on the remote back to the local pane.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import socket
import stat
import time
from pathlib import Path
from typing import Any

MAX_HEADER = 64 * 1024
NO_PANE = (
    "mediapane: no pane running. Start one with `mediapane open` (or `mediapane` in a "
    "spare pane); on a server, connect with `mediapane ssh <host>`."
)
CHUNK = 1 << 20


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "mediapane"


def host_tag() -> str:
    """This host's name, safe for file names."""
    return re.sub(r"[^A-Za-z0-9.\-]", "_", socket.gethostname()) or "host"


def default_socket() -> Path:
    """Per host: the cache dir may be on a home shared by many machines (NFS on clusters)."""
    env = os.environ.get("MEDIAPANE_SOCKET")
    return Path(env) if env else cache_dir() / f"pane-{host_tag()}.sock"


def forwarded_sockets() -> list[Path]:
    """Sockets `mediapane ssh` forwarded to this host, newest first. Only sockets owned by us:
    /tmp is shared, and anyone could create one under our name to collect what we push."""
    found = []
    for p in Path("/tmp").glob(f"mediapane-{getpass.getuser()}-*.sock"):
        try:
            st = p.lstat()
        except OSError:
            continue
        if stat.S_ISSOCK(st.st_mode) and st.st_uid == os.getuid():
            found.append((st.st_mtime, p))
    return [p for _, p in sorted(found, reverse=True)]


def candidate_sockets() -> list[Path]:
    """Where a pane might be listening: $MEDIAPANE_SOCKET, a pane on this host, forwarded ones."""
    if os.environ.get("MEDIAPANE_SOCKET"):
        return [default_socket()]
    return [default_socket(), *forwarded_sockets()]


def request(
    op: str,
    data: bytes = b"",
    *,
    source: Path | None = None,
    path: Path | None = None,
    timeout: float = 120.0,
    **meta: Any,
) -> dict[str, Any] | None:
    """Send one request to the first pane that answers; None if no pane is reachable. The payload
    is `data`, or the file `source`, streamed in chunks so large videos never sit in memory.
    The timeout covers the pane decoding what it was sent (a long GIF can take a while)."""
    try:
        size = source.stat().st_size if source is not None else len(data)
    except OSError:
        return {"ok": False, "error": f"can't read {source}"}
    header = json.dumps({"op": op, "size": size, "host": socket.gethostname(), **meta})
    for sock_path in [path] if path else candidate_sockets():
        sent = False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect(str(sock_path))
                s.sendall(header.encode() + b"\n")
                if source is not None:
                    with open(source, "rb") as f:
                        while chunk := f.read(CHUNK):
                            s.sendall(chunk)
                else:
                    s.sendall(data)
                sent = True
                reply = json.loads(_readline(s) or b"null")
        except TimeoutError:
            if sent:  # it has it and may still show it: trying elsewhere would duplicate it
                return {
                    "ok": False,
                    "error": "the pane didn't answer in time (it may still show it)",
                }
            continue
        except ConnectionRefusedError:
            _drop_stale(sock_path)
            continue
        except (OSError, ValueError):
            continue
        if reply:  # a forwarded socket accepts even when the pane behind it is gone
            return reply
    if op == "push" and path is None and (spool := spool_dir()):
        return _spool(spool, header, data, source)
    return None


def spool_dir() -> Path | None:
    env = os.environ.get("MEDIAPANE_SPOOL")
    return Path(env).expanduser() if env else None


def _spool(spool: Path, header: str, data: bytes, source: Path | None) -> dict[str, Any]:
    """Leave a push for a relay: the wire format in a file, renamed into place when complete."""
    try:
        spool.mkdir(mode=0o700, parents=True, exist_ok=True)
        stem = f"{time.time_ns()}-{host_tag()}-{os.getpid()}"
        tmp = spool / f"{stem}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(header.encode() + b"\n")
            if source is not None:
                with open(source, "rb") as src:
                    while chunk := src.read(CHUNK):
                        f.write(chunk)
            else:
                f.write(data)
        final = tmp.with_suffix(".mp")
        os.replace(tmp, final)
    except OSError as e:
        return {"ok": False, "error": f"can't spool to {spool}: {e.strerror}"}
    return {"ok": True, "spooled": str(final)}


def push(data: bytes, name: str, **meta: Any) -> bool:
    reply = request("push", data, name=name, **meta)
    return bool(reply and reply.get("ok"))


def push_file(source: Path, name: str | None = None, **meta: Any) -> dict[str, Any] | None:
    """Stream a file to the pane; the pane's reply, or None if no pane is reachable."""
    return request("push", source=source, name=name or source.name, **meta)


def _readline(s: socket.socket) -> bytes:
    buf = b""
    while not buf.endswith(b"\n") and len(buf) < MAX_HEADER:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf.strip()


def _drop_stale(sock_path: Path) -> None:
    """Remove a dead forwarded socket we own (sshd leaves them behind when sessions end)."""
    if sock_path.parent == Path("/tmp") and sock_path.name.startswith("mediapane-"):
        try:
            if sock_path.stat().st_uid == os.getuid():
                sock_path.unlink()
        except OSError:
            pass
