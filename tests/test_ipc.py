from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from mediapane import cli, ipc, remote


class FakePane:
    """Accepts pushes on a socket and records them."""

    def __init__(self, path: Path) -> None:
        self.path = path
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
                while len(rest) < header["size"]:
                    rest += conn.recv(65536)
                self.received.append((header, rest))
                conn.sendall(b'{"ok": true}\n')

    def close(self) -> None:
        self.srv.close()


@pytest.fixture
def short_dir():
    d = Path(tempfile.mkdtemp(prefix="mp"))
    yield d


def test_request_reaches_pane(short_dir, monkeypatch):
    fake = FakePane(short_dir / "s")
    monkeypatch.setenv("MEDIAPANE_SOCKET", str(fake.path))
    assert ipc.push(b"\x89PNGdata", "x.png")
    header, data = fake.received[0]
    assert header["op"] == "push" and header["name"] == "x.png" and data == b"\x89PNGdata"
    assert header["host"] == socket.gethostname()
    fake.close()


def test_no_pane(short_dir, monkeypatch):
    monkeypatch.setenv("MEDIAPANE_SOCKET", str(short_dir / "nothing"))
    assert ipc.request("ping") is None
    assert not ipc.push(b"x", "x.png")


def test_dead_forwarded_socket_is_cleaned_up(monkeypatch):
    user = f"pytest{os.getpid()}"
    monkeypatch.setattr(ipc.getpass, "getuser", lambda: user)
    monkeypatch.delenv("MEDIAPANE_SOCKET", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", tempfile.mkdtemp(prefix="mp"))
    dead = Path(f"/tmp/mediapane-{user}-dead.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(dead))
    s.close()  # bound but nobody listening: connect is refused
    assert dead in ipc.forwarded_sockets()
    assert ipc.request("ping") is None
    assert not dead.exists()


def test_forwarded_socket_is_found(monkeypatch):
    user = f"pytest{os.getpid()}"
    monkeypatch.setattr(ipc.getpass, "getuser", lambda: user)
    monkeypatch.delenv("MEDIAPANE_SOCKET", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", tempfile.mkdtemp(prefix="mp"))
    fake = FakePane(Path(f"/tmp/mediapane-{user}-live.sock"))
    try:
        assert ipc.push(b"abc", "a.png")
        assert fake.received
    finally:
        fake.close()
        fake.path.unlink()


def test_cli_push_and_errors(short_dir, monkeypatch, png, capsys):
    monkeypatch.setenv("MEDIAPANE_SOCKET", str(short_dir / "none"))
    assert cli.main(["push", str(png)]) == 1
    assert "no pane running" in capsys.readouterr().err
    fake = FakePane(short_dir / "s")
    monkeypatch.setenv("MEDIAPANE_SOCKET", str(fake.path))
    assert cli.main([str(png)]) == 0  # bare file argument means push
    assert cli.main(["push", str(short_dir / "missing.png")]) == 1
    assert fake.received[0][0]["name"] == "plot.png"
    fake.close()


def test_matplotlib_backend(short_dir, monkeypatch):
    matplotlib = pytest.importorskip("matplotlib")
    fake = FakePane(short_dir / "s")
    monkeypatch.setenv("MEDIAPANE_SOCKET", str(fake.path))
    matplotlib.use("module://mediapane.mpl")
    import matplotlib.pyplot as plt

    plt.figure("loss")
    plt.plot([3, 2, 1])
    plt.show()
    assert plt.get_fignums() == []  # sent, then closed
    header, data = fake.received[0]
    assert header["name"] == "loss.png" and data.startswith(b"\x89PNG")
    matplotlib.use("Agg")
    fake.close()


def test_remote_user_from_ssh_config(monkeypatch):
    class R:
        stdout = "hostname gpu03\nuser alice\nport 22\n"

    monkeypatch.setattr(remote.subprocess, "run", lambda *a, **k: R())
    assert remote.remote_user(["gpu03"]) == "alice"
