from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest


class FakePane:
    """Accepts requests on a Unix socket, records them, and answers {"ok": <ok>}."""

    def __init__(self, path: Path, ok: bool = True) -> None:
        self.path, self.ok = path, ok
        self.received: list[tuple[dict, bytes]] = []
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(str(path))
        self.srv.listen()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            with conn:
                buf = b""
                while b"\n" not in buf:
                    buf += conn.recv(4096)
                line, rest = buf.split(b"\n", 1)
                header = json.loads(line)
                while len(rest) < header.get("size", 0):
                    rest += conn.recv(65536)
                self.received.append((header, rest))
                reply = {"ok": self.ok} if self.ok else {"ok": False, "error": "bad image"}
                conn.sendall(json.dumps(reply).encode() + b"\n")

    def close(self) -> None:
        self.srv.close()


@pytest.fixture
def short_dir():
    # macOS caps socket paths at ~104 bytes, too short for pytest's tmp_path
    d = Path(tempfile.mkdtemp(prefix="mp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """No personal plugins, no spool, and no real pane reachable unless a test adds one."""
    monkeypatch.setenv("MEDIAPANE_PLUGINS", str(tmp_path / "no-plugins"))
    monkeypatch.delenv("MEDIAPANE_NO_PLUGINS", raising=False)
    monkeypatch.delenv("MEDIAPANE_SPOOL", raising=False)
    monkeypatch.setenv("MEDIAPANE_SOCKET", str(tmp_path / "no-pane.sock"))


@pytest.fixture
def start_pane(short_dir, monkeypatch):
    """Start a fake pane partway through a test (e.g. after pushes were spooled)."""
    started: list[FakePane] = []

    def start(ok: bool = True) -> FakePane:
        fake = FakePane(short_dir / f"s{len(started)}", ok)
        monkeypatch.setenv("MEDIAPANE_SOCKET", str(fake.path))
        started.append(fake)
        return fake

    yield start
    for fake in started:
        fake.close()


@pytest.fixture
def pane(start_pane):
    return start_pane()
