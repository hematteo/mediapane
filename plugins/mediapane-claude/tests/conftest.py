from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest


class FakePane:
    """Accepts pushes on a socket and records them."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.received: list[tuple[dict, bytes]] = []
        self.reply = b'{"ok": true}\n'
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
                while len(rest) < header["size"]:
                    rest += conn.recv(65536)
                self.received.append((header, rest))
                conn.sendall(self.reply)

    @property
    def names(self) -> list[str]:
        return [h["name"] for h, _ in self.received]

    def close(self) -> None:
        self.srv.close()


@pytest.fixture
def short_dir():
    # macOS caps Unix socket paths at ~104 bytes: pytest's tmp_path is too long
    d = Path(tempfile.mkdtemp(prefix="mp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(autouse=True)
def isolated(short_dir, tmp_path, monkeypatch):
    """Never reach the user's real pane, spool or Claude settings."""
    monkeypatch.setenv("MEDIAPANE_SOCKET", str(short_dir / "nobody"))
    monkeypatch.delenv("MEDIAPANE_SPOOL", raising=False)
    monkeypatch.delenv("MEDIAPANE_DEBUG", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))  # no personal plugins
    monkeypatch.delenv("MEDIAPANE_PLUGINS", raising=False)
    monkeypatch.delenv("MEDIAPANE_NO_PLUGINS", raising=False)


@pytest.fixture
def pane(short_dir, monkeypatch):
    fake = FakePane(short_dir / "s")
    monkeypatch.setenv("MEDIAPANE_SOCKET", str(fake.path))
    yield fake
    fake.close()
