from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from mediapane import cli, ipc, plugins

from mediapane_relay import relay as relay_mod


@pytest.fixture
def spool(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "spool"
    monkeypatch.setenv("MEDIAPANE_SPOOL", str(d))
    return d


def spool_push(data: bytes, name: str, **meta) -> Path:
    """A push from a compute node: no pane reachable, so the core spools it."""
    reply = ipc.request("push", data, name=name, **meta)
    assert reply and reply.get("spooled")
    return Path(reply["spooled"])


def write_mp(spool: Path, stem: str, data: bytes, name: str) -> Path:
    spool.mkdir(parents=True, exist_ok=True)
    header = json.dumps({"op": "push", "size": len(data), "host": "gpu07", "name": name})
    p = spool / f"{stem}.mp"
    p.write_bytes(header.encode() + b"\n" + data)
    return p


def relay(*args: str) -> int:
    return cli.main(["relay", *args])


def listing(d: Path) -> set[str]:
    return {str(p.relative_to(d)) for p in d.rglob("*")}


def test_plugin_registers_command():
    api = plugins.load()
    assert "relay" in api.loaded and api.commands["relay"].plugin == "relay"


def test_forwards_and_deletes(spool, start_pane, capsys):
    spool_push(b"\x89PNG one", "a.png", tag="samples", step="100")
    big = os.urandom(3 * ipc.CHUNK + 17)  # several chunks
    spool_push(big, "b.mp4")
    fake = start_pane()
    assert relay("--once") == 0
    (h1, d1), (h2, d2) = fake.received
    assert (h1["name"], h1["tag"], h1["step"], d1) == ("a.png", "samples", "100", b"\x89PNG one")
    assert h2["name"] == "b.mp4" and d2 == big
    assert list(spool.glob("*.mp")) == []
    out = capsys.readouterr().out
    assert out.count("forwarded") == 2 and "a.png from" in out


def test_keeps_files_without_a_pane_and_never_respools(spool, capsys):
    spool_push(b"one", "a.png")
    spool_push(b"two", "b.png")
    before = {p: p.read_bytes() for p in spool.iterdir()}
    assert relay("--once") == 1
    assert relay("--once", "--spool", str(spool)) == 1
    assert {p: p.read_bytes() for p in spool.iterdir()} == before  # nothing new, nothing lost
    assert "waiting for a pane (2 queued" in capsys.readouterr().out


def test_waiting_is_said_once(spool, start_pane, capsys):
    spool_push(b"one", "a.png")
    r = relay_mod.Relay(spool)
    assert not r.run_once() and not r.run_once() and not r.run_once()
    assert capsys.readouterr().out.count("waiting for a pane") == 1
    fake = start_pane()
    assert r.run_once() and len(fake.received) == 1
    assert "forwarded" in capsys.readouterr().out


def test_rejected_moves_to_failed(spool, pane, capsys):
    pane.ok = False
    p = write_mp(spool, "100-gpu07-1", b"junk", "bad.png")
    write_mp(spool, "200-gpu07-1", b"x", "next.png")
    assert relay("--once") == 0
    assert (spool / "failed" / p.name).exists()
    assert listing(spool) == {"failed", "failed/100-gpu07-1.mp", "failed/200-gpu07-1.mp"}
    out = capsys.readouterr().out
    assert "rejected bad.png from gpu07 (4 B): bad image" in out


def test_malformed_and_truncated_files_go_to_failed(spool, pane, capsys):
    spool.mkdir()
    (spool / "1-h-1.mp").write_bytes(b"this is not json\nxx")
    (spool / "2-h-1.mp").write_bytes(b'{"op": "clear", "size": 0}\n')
    good = write_mp(spool, "3-h-1", b"abcdef", "t.png")
    good.write_bytes(good.read_bytes()[:-2])  # shorter than its header says
    assert relay("--once") == 0
    assert pane.received == []
    assert listing(spool / "failed") == {"1-h-1.mp", "2-h-1.mp", "3-h-1.mp"}
    assert "size doesn't match" in capsys.readouterr().out


def test_ignores_fresh_tmp_and_cleans_old_tmp(spool, pane, capsys):
    spool.mkdir()
    fresh = spool / "5-h-1.tmp"
    fresh.write_bytes(b'{"op": "push", "size": 3, "name": "x.png"}\nabc')
    old = spool / "1-h-1.tmp"
    old.write_bytes(b"partial")
    hours_ago = time.time() - 2 * 3600
    os.utime(old, (hours_ago, hours_ago))
    assert relay("--once") == 0
    assert pane.received == []
    assert fresh.exists() and not old.exists()
    assert "removed 1-h-1.tmp" in capsys.readouterr().out


def test_oldest_first(spool, pane):
    # created out of order, and with time stamps of different lengths: order is numeric
    for stem in ("999-h-1", "1000-h-1", "50-h-2", "1000-g-3"):
        write_mp(spool, stem, stem.encode(), f"{stem}.png")
    assert relay("--once") == 0
    assert [d.decode() for _, d in pane.received] == ["50-h-2", "999-h-1", "1000-g-3", "1000-h-1"]


def test_core_spool_names_sort_in_push_order(spool, start_pane):
    for i in range(5):
        spool_push(str(i).encode(), f"{i}.png")
    pane = start_pane()
    assert relay("--once") == 0
    assert [d for _, d in pane.received] == [b"0", b"1", b"2", b"3", b"4"]


def test_needs_a_spool(monkeypatch, capsys):
    monkeypatch.delenv("MEDIAPANE_SPOOL", raising=False)
    assert relay("--once") == 2
    assert "MEDIAPANE_SPOOL" in capsys.readouterr().err


def test_loop_stops_quietly_on_ctrl_c(spool, pane, monkeypatch, capsys):
    write_mp(spool, "1-h-1", b"x", "x.png")

    def interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(relay_mod.time, "sleep", interrupt)
    assert relay("--interval", "0.1") == 0
    assert len(pane.received) == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err and "relaying pushes from" in captured.out


def test_two_relays_deliver_each_push_once(spool, pane):
    import threading

    for i in range(40):
        write_mp(spool, f"{1000 + i}-node-1", b"x" * 10, f"f{i}.png")
    relays = [relay_mod.Relay(spool), relay_mod.Relay(spool)]
    threads = [threading.Thread(target=r.run_once) for r in relays]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    names = [h["name"] for h, _ in pane.received]
    assert sorted(names) == sorted(f"f{i}.png" for i in range(40))  # each exactly once
    assert listing(spool) == set()


def test_a_dead_relays_claim_goes_back_in_the_queue(spool, pane):
    p = write_mp(spool, "1000-node-1", b"x", "a.png")
    stuck = p.with_suffix(".sending")
    os.rename(p, stuck)
    old = time.time() - 2 * relay_mod.STALE_TMP
    os.utime(stuck, (old, old))
    relay_mod.Relay(spool).run_once()  # recovers it this round...
    relay_mod.Relay(spool).run_once()  # ...and forwards it
    assert [h["name"] for h, _ in pane.received] == ["a.png"]


def test_only_your_own_files_are_forwarded(spool, pane, monkeypatch, capsys):
    write_mp(spool, "1000-node-1", b"x", "mine.png")
    real_uid = os.getuid()
    monkeypatch.setattr(relay_mod.os, "getuid", lambda: real_uid + 1)  # as if someone else's
    relay_mod.Relay(spool).run_once()
    assert pane.received == []
    assert "not yours" in capsys.readouterr().out
