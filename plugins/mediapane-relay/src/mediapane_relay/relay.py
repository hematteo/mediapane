"""Forward spooled pushes to a pane.

Each `.mp` file in the spool is one push, already in wire format (JSON header line + payload),
so it is streamed to the pane's socket as is. Sockets are always named explicitly: a plain
`ipc.request` with $MEDIAPANE_SPOOL set would spool the push again when no pane answers.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any

from mediapane import ipc
from mediapane.layout import clean

STALE_TMP = 3600.0  # a .tmp this old belongs to a writer that died
TIMEOUT = 120.0  # as ipc.request: covers the pane decoding a long GIF


def main(spool: Path | None, *, interval: float = 0.5, once: bool = False) -> int:
    spool = spool or ipc.spool_dir()
    if spool is None:
        print(
            "mediapane relay: no spool folder. Pass --spool DIR or set MEDIAPANE_SPOOL to a "
            "folder that both your jobs and this machine can see (e.g. on a shared home).",
            file=sys.stderr,
        )
        return 2
    if interval <= 0:
        print("mediapane relay: --interval must be positive", file=sys.stderr)
        return 2
    try:
        spool.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as e:
        print(f"mediapane relay: can't use {spool}: {e.strerror}", file=sys.stderr)
        return 2
    relay = Relay(spool)
    if once:
        return 0 if relay.run_once() else 1
    print(f"relaying pushes from {spool} (Ctrl-C to stop)", flush=True)
    try:
        while True:
            relay.run_once()
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


class Relay:
    def __init__(self, spool: Path) -> None:
        self.spool = spool
        self.failed = spool / "failed"
        self.waiting = False  # said "waiting for a pane" since the last delivery
        self.skipped: set[str] = set()  # others' files, reported once
        try:
            if spool.stat().st_mode & 0o022:
                print(
                    f"warning: {spool} is writable by others; only your own files are "
                    "forwarded, but a private folder (chmod 700) is safer",
                    flush=True,
                )
        except OSError:
            pass

    def pending(self) -> list[Path]:
        """Complete pushes of ours, oldest first: names start with the writer's time_ns. On a
        shared filesystem anyone who can write the folder could drop files in; only files
        owned by us are forwarded."""
        mine = []
        for p in self.spool.glob("*.mp"):
            try:
                owner = p.stat().st_uid
            except OSError:
                continue
            if owner == os.getuid():
                mine.append(p)
            elif p.name not in self.skipped:
                self.skipped.add(p.name)
                print(f"skipped {p.name}: not yours (owner uid {owner})", flush=True)
        return sorted(mine, key=_order)

    def run_once(self) -> bool:
        """Forward everything waiting; False if some is left because no pane is reachable."""
        self.clean_tmp()
        pending = self.pending()
        for i, path in enumerate(pending):
            # Claim it with an atomic rename: of two relays on one spool, only one wins
            claimed = path.with_suffix(".sending")
            try:
                os.rename(path, claimed)
                os.utime(claimed)  # claim time: what stale-claim recovery measures
            except FileNotFoundError:
                continue
            header, reply = self.forward(claimed)
            what = _describe(header, path)
            if reply is None:
                with contextlib.suppress(FileNotFoundError):
                    os.rename(claimed, path)  # back in the queue
                if not self.waiting:
                    n = len(pending) - i
                    print(f"waiting for a pane ({n} queued in {self.spool})", flush=True)
                    self.waiting = True
                return False
            if reply.get("gone"):
                continue
            self.waiting = False
            if reply.get("ok"):
                _unlink(claimed)
                print(f"forwarded {what}", flush=True)
            else:
                error = clean(reply.get("error", "rejected"))
                print(f"rejected {what}: {error}; moved to {self.failed}/", flush=True)
                self.fail(claimed, path.name)
        return True

    def forward(self, path: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """(header, the pane's reply); the reply is None if no pane is reachable."""
        try:
            f = open(path, "rb")
        except FileNotFoundError:
            return None, {"gone": True}
        except OSError as e:
            return None, {"ok": False, "error": f"can't read it: {e.strerror}"}
        with f:
            line = f.readline(ipc.MAX_HEADER + 1)
            header = _header(line)
            if header is None:
                return None, {"ok": False, "error": "not a mediapane push"}
            if os.fstat(f.fileno()).st_size != len(line) + header["size"]:
                return header, {"ok": False, "error": "size doesn't match its header"}
            for sock_path in ipc.candidate_sockets():
                f.seek(0)
                try:
                    reply = _send(sock_path, f)
                except (OSError, ValueError):
                    continue
                if isinstance(reply, dict):  # a forwarded socket may have no pane behind it
                    return header, reply
        return header, None

    def fail(self, path: Path, name: str) -> None:
        try:
            self.failed.mkdir(mode=0o700, exist_ok=True)
            os.replace(path, self.failed / name)
        except FileNotFoundError:
            pass
        except OSError as e:  # can't keep it: drop it rather than retry it forever
            print(f"  can't move it ({e.strerror}); deleting it", flush=True)
            _unlink(path)

    def clean_tmp(self) -> None:
        now = time.time()
        for p in self.spool.glob("*.sending"):  # claimed by a relay that died mid-send
            try:
                if now - p.stat().st_mtime > STALE_TMP:
                    os.rename(p, p.with_suffix(".mp"))
            except OSError:
                continue
        for p in self.spool.glob("*.tmp"):
            try:
                if now - p.stat().st_mtime > STALE_TMP:
                    p.unlink()
                    print(f"removed {p.name}: a partial push from a writer that died", flush=True)
            except OSError:
                continue


def _send(sock_path: Path, f: Any) -> Any:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(TIMEOUT)
        s.connect(str(sock_path))
        while chunk := f.read(ipc.CHUNK):
            s.sendall(chunk)
        buf = b""
        while not buf.endswith(b"\n") and len(buf) < ipc.MAX_HEADER:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.strip() or b"null")


def _header(line: bytes) -> dict[str, Any] | None:
    if not line.endswith(b"\n"):
        return None
    try:
        header = json.loads(line)
    except ValueError:
        return None
    if not isinstance(header, dict) or header.get("op") != "push":
        return None
    size = header.get("size")
    return header if isinstance(size, int) and size >= 0 else None


def _order(path: Path) -> tuple[int, str]:
    stamp = path.name.split("-", 1)[0]
    return (int(stamp) if stamp.isdigit() else sys.maxsize, path.name)


def _describe(header: dict[str, Any] | None, path: Path) -> str:
    if header is None:
        return path.name
    what = f"{clean(header.get('name') or path.name, 80)} from {clean(header.get('host', '?'), 60)}"
    return f"{what} ({_size(header['size'])})"


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1000:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} GB"


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
