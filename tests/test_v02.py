"""v0.2: plugins, series, zoom, native animation, spool, history."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import textwrap
import time
from pathlib import Path

import pytest
from PIL import Image
from test_pane import PtyPane, placements
from test_pane import pane as pane  # noqa: F401 - the fixture, shared

from mediapane import cli, ipc, plugins
from mediapane.kitty import Graphics
from mediapane.media import cropped_png, probe
from mediapane.pane import Item, Pane
from mediapane.watch import series_key


@pytest.fixture
def fresh_plugins(monkeypatch, tmp_path):
    """A clean plugin registry reading only `tmp_path/plugins`."""
    d = tmp_path / "plugins"
    d.mkdir()
    monkeypatch.setenv("MEDIAPANE_PLUGINS", str(d))
    monkeypatch.delenv("MEDIAPANE_NO_PLUGINS", raising=False)
    monkeypatch.setattr(plugins, "_api", None)
    monkeypatch.setattr(plugins, "_sources", _local_only(plugins._sources))
    return d


def _local_only(sources):
    def only_files():  # ignore entry points of whatever plugins happen to be installed
        return [s for s in sources() if "(" in s[0]]

    return only_files


def test_local_plugin_registers_command_key_and_hook(fresh_plugins, capsys):
    (fresh_plugins / "hello.py").write_text(
        textwrap.dedent("""
        def register(api):
            api.command("hello", "say hello", run=lambda args: print("hi", args.who) or 0,
                        setup=lambda p: p.add_argument("who"))
            api.key("y", "yank", lambda pane: pane.say("yanked"))
            api.key("q", "steal quit", lambda pane: None)  # core key: must not win
            api.on_item(lambda item: None)
    """)
    )
    (fresh_plugins / "broken.py").write_text("raise RuntimeError('boom')\n")
    api = plugins.load()
    assert "hello" in api.commands and "y" in api.keys and "q" not in api.keys
    errors = dict(api.errors)
    assert "is taken by mediapane" in errors[f"hello ({fresh_plugins / 'hello.py'})"]
    assert "boom" in errors[f"broken ({fresh_plugins / 'broken.py'})"]
    assert cli.main(["hello", "world"]) == 0
    assert capsys.readouterr().out.strip() == "hi world"
    assert plugins.report() == 1  # a failed plugin is reported, not fatal


def test_plugins_off(fresh_plugins, monkeypatch):
    (fresh_plugins / "hello.py").write_text("def register(api): api.command('x', 'x', run=print)")
    monkeypatch.setenv("MEDIAPANE_NO_PLUGINS", "1")
    assert plugins.load().commands == {}


def test_pane_runs_plugin_keys_and_hooks(fresh_plugins, png, tmp_path):
    seen = tmp_path / "seen.txt"
    (fresh_plugins / "spy.py").write_text(
        textwrap.dedent(f"""
        def register(api):
            api.on_item(lambda item: open({str(seen)!r}, "a").write(item.name + "\\n"))
            api.key("y", "yank", lambda pane: pane.say("yanked " + pane.selected.name))
    """)
    )
    p = PtyPane(tmp_path, extra_env={"MEDIAPANE_PLUGINS": str(fresh_plugins)})
    try:
        p.push(png)
        p.wait_for(lambda: seen.exists() and "plot.png" in seen.read_text(), "item hook")
        p.keys("y")
        p.wait_for(lambda: "yanked plot.png" in p.text(), "plugin key")
        p.keys("?")
        p.wait_for(lambda: "[spy" in p.text(), "plugin key in help")
    finally:
        p.close()


def _push_series(p: PtyPane, png: Path, steps: list[int], tag: str = "samples") -> list[dict]:
    return [ipc.request("push", png.read_bytes(), path=p.sock, name=png.name, tag=tag, step=s)
            for s in steps]  # fmt: skip


def test_series_is_one_entry_stepped_with_arrows(pane, png):
    replies = _push_series(pane, png, [100, 200, 300])
    assert all(r["ok"] for r in replies)
    assert len({r["id"] for r in replies}) == 1  # one gallery entry, one image id
    assert ipc.request("ping", path=pane.sock)["items"] == 1
    pane.wait_for(lambda: "step 300" in pane.text() and "3/3" in pane.text(), "newest step")
    pane.keys("\x1b[D")  # ←
    pane.wait_for(lambda: "step 200" in pane.text().rsplit("\x1b[?2026h", 1)[-1], "step back")
    _push_series(pane, png, [400])  # stepped back: stays on 200
    time.sleep(0.4)
    assert "step 200" in pane.text().rsplit("\x1b[?2026h", 1)[-1]
    pane.keys(">")
    pane.wait_for(lambda: "step 400" in pane.text().rsplit("\x1b[?2026h", 1)[-1], "last step")


def test_time_lapse_swaps_images_under_one_id(pane, png, tmp_path):
    other = tmp_path / "other.png"
    Image.new("RGB", (300, 200), (20, 200, 20)).save(other)
    image_id = _push_series(pane, png, [1])[0]["id"]
    _push_series(pane, other, [2])
    pane.wait_for(lambda: "2/2" in pane.text(), "two steps")
    before = len(placements(pane.text(), image_id))
    pane.keys("t")
    pane.wait_for(lambda: len(placements(pane.text(), image_id)) >= before + 4, "lapse frames")
    assert "time-lapse" in pane.text()


def test_cropped_png(png):
    data = cropped_png(png, 120, 80, 2.0, 0.5, 0.5)
    import io

    with Image.open(io.BytesIO(data)) as im:
        assert im.size == (120, 80)


def test_zoom_in_large_view(pane, png):
    image_id = pane.push(png)["id"]
    pane.wait_for(lambda: placements(pane.text(), image_id), "upload")
    pane.keys("\r")
    time.sleep(0.3)
    n = len(placements(pane.text(), image_id))
    pane.keys("+")
    pane.wait_for(lambda: "zoom 1.5×" in pane.text(), "zoom caption")
    pane.wait_for(lambda: len(placements(pane.text(), image_id)) > n, "cropped upload")
    assert "t=d" in placements(pane.text(), image_id)[-1]  # the crop is sent as data


def test_native_animation_uploads_frames_once(tmp_path, gif):
    pane = Pane(tmp_path / "unused.sock")
    pane.gfx = Graphics(direct=True, tmux=False)
    pane.native_anim = True
    media = probe(gif, tmp_path / "w")
    it = Item(pane.new_id(), "anim.gif", "", media, tmp_path / "w", 0.0)
    pane.items.append(it)
    assert pane.upload(it, (10, 5)) == ""  # frames still being scaled on workers
    pane.pool.shutdown(wait=True)
    pane.collect_scaled()
    out = pane.upload(it, (10, 5))
    assert out.count("a=f,q=2,") == len(media.frames) - 1
    assert f"a=a,q=2,i={it.id},s=3,v=1" in out
    assert it.native and pane.upload(it, (10, 5)) == ""  # stored: nothing more to send
    assert pane.tick(time.monotonic() + 100) == ""  # and no per-frame traffic


def test_spool_when_no_pane(tmp_path, monkeypatch, png):
    monkeypatch.setenv("MEDIAPANE_SOCKET", str(Path(tempfile.mkdtemp(prefix="mp")) / "none"))
    monkeypatch.setenv("MEDIAPANE_SPOOL", str(tmp_path / "spool"))
    reply = ipc.push_file(png, tag="t", step="3")
    assert reply["ok"] and reply["spooled"]
    (spooled,) = (tmp_path / "spool").glob("*.mp")
    header, payload = spooled.read_bytes().split(b"\n", 1)
    meta = json.loads(header)
    assert meta["name"] == "plot.png" and meta["tag"] == "t" and meta["step"] == "3"
    assert payload == png.read_bytes() and meta["size"] == len(payload)
    assert oct(spooled.stat().st_mode & 0o777) == "0o600"
    assert not list((tmp_path / "spool").glob("*.tmp"))


def test_watch_series_keys():
    assert series_key(Path("samples_001000.png")) == ("samples_#.png", "1000")
    assert series_key(Path("fig.png")) is None


def test_cli_push_tag_and_step(monkeypatch, png):
    got = {}

    def fake(path, name=None, **meta):
        got.update(meta)
        return {"ok": True}

    monkeypatch.setattr(ipc, "push_file", fake)
    assert cli.main(["push", "--tag", "loss", "--step", "7", str(png)]) == 0
    assert got == {"tag": "loss", "step": "7"}


def test_history_and_resume(tmp_path, png, gif):
    p = PtyPane(tmp_path)
    p.push(png)
    _push_series(p, png, [1, 2])
    p.push(gif)
    p.wait_for(lambda: ipc.request("ping", path=p.sock)["items"] == 3, "items")
    assert p.close() == 0
    hist = next((tmp_path / "cache" / "mediapane").glob("history-*"))
    recs = [json.loads(line) for line in (hist / "items.jsonl").read_text().splitlines()]
    assert len(recs) == 4 and [r.get("step") for r in recs] == [None, "1", "2", None]

    p2 = PtyPane(tmp_path, args=["--resume"])
    try:
        p2.wait_for(lambda: ipc.request("ping", path=p2.sock)["items"] == 3, "resumed items")
        p2.wait_for(lambda: "samples" in p2.text() and "2/2" in p2.text(), "series resumed")
    finally:
        p2.close()


def test_socket_and_session_are_per_host(monkeypatch):
    monkeypatch.delenv("MEDIAPANE_SOCKET", raising=False)
    assert ipc.default_socket().name == f"pane-{ipc.host_tag()}.sock"
    assert socket.gethostname() and os.sep not in ipc.host_tag()


def test_plugin_files_others_can_write_are_refused(fresh_plugins):
    f = fresh_plugins / "shared.py"
    f.write_text("def register(api): api.command('shared', 'x', run=print)\n")
    f.chmod(0o666)
    api = plugins.load()
    assert "shared" not in api.commands
    assert "writable by others" in dict(api.errors)[f"shared ({f})"]


def test_series_tags_keep_runs_apart(tmp_path):
    a = tmp_path / "run_a" / "samples_100.png"
    assert series_key(a, tmp_path) == ("run_a/samples_#.png", "100")
    assert series_key(tmp_path / "samples_100.png", tmp_path) == ("samples_#.png", "100")


def test_plugin_command_beats_a_same_named_file(fresh_plugins, tmp_path, monkeypatch, capsys):
    (fresh_plugins / "hello.py").write_text(
        "def register(api): api.command('hello', 'x', run=lambda a: print('ran') or 0)\n"
    )
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hello").write_text("not media")  # e.g. a folder or file named like the command
    assert cli.main(["hello"]) == 0
    assert capsys.readouterr().out.strip() == "ran"


def _native_pane(tmp_path):
    pane = Pane(tmp_path / "unused.sock")
    pane.gfx = Graphics(direct=True, tmux=False)
    pane.native_anim = True
    pane.written = []
    pane.write = pane.written.append  # keep the terminal output out of pytest's
    return pane


def _settle(pane, it, cells):
    out = pane.upload(it, cells)
    for _ in range(50):
        if it.native:
            return out
        time.sleep(0.05)
        pane.collect_scaled()
        out = pane.upload(it, cells)
    raise AssertionError("native upload never finished")


def test_native_series_step_sends_the_new_animation(tmp_path, gif):
    from mediapane.pane import Step

    pane = _native_pane(tmp_path)
    a, b = probe(gif, tmp_path / "a"), probe(gif, tmp_path / "b")
    steps = [Step(a, tmp_path / "a", "1", "", 0.0), Step(b, tmp_path / "b", "2", "", 0.0)]
    it = Item(pane.new_id(), "s", "", a, tmp_path / "a", 0.0, tag="s", steps=steps)
    pane.items.append(it)
    _settle(pane, it, (10, 5))
    pane.show_step(it, 1)
    assert not it.native and it.id not in pane.sent  # the terminal's copy is stale now
    assert "a=f" in _settle(pane, it, (10, 5))  # the new step's frames are sent


def test_pausing_a_native_animation_keeps_it_in_the_terminal(tmp_path, gif):
    pane = _native_pane(tmp_path)
    it = Item(pane.new_id(), "g", "", probe(gif, tmp_path / "w"), tmp_path / "w", 0.0)
    pane.items.append(it)
    _settle(pane, it, (10, 5))
    pane.toggle_play(it)
    assert pane.written[-1].endswith(f"a=a,q=2,i={it.id},s=1\x1b\\")
    assert pane.upload(it, (10, 5)) == ""  # no re-upload while paused


def test_too_big_for_the_terminal_falls_back_to_streaming(tmp_path, gif, monkeypatch):
    import mediapane.pane.draw as pane_mod

    monkeypatch.setattr(pane_mod, "NATIVE_MAX_PIXELS", 10)
    pane = _native_pane(tmp_path)
    it = Item(pane.new_id(), "g", "", probe(gif, tmp_path / "w"), tmp_path / "w", 0.0)
    pane.items.append(it)
    pane.upload(it, (10, 5))
    assert it.native_failed and not pane.wants_native(it) and not pane.native_jobs


def test_timeout_after_sending_is_an_error_not_a_retry(tmp_path, monkeypatch):
    sock_dir = Path(tempfile.mkdtemp(prefix="mp"))
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_dir / "s"))
    srv.listen()  # accepts, never answers
    monkeypatch.setenv("MEDIAPANE_SOCKET", str(sock_dir / "s"))
    monkeypatch.setenv("MEDIAPANE_SPOOL", str(tmp_path / "spool"))
    reply = ipc.request("push", b"x", name="x.png", timeout=0.3)
    assert reply == {"ok": False, "error": "the pane didn't answer in time (it may still show it)"}
    assert not (tmp_path / "spool").exists()  # not spooled as well: that would duplicate it
    srv.close()


def test_history_is_private_keeps_pending_and_respects_clear(tmp_path, png):
    p = PtyPane(tmp_path)
    p.push(png)
    assert p.close() == 0
    hist = next((tmp_path / "cache" / "mediapane").glob("history-*"))
    assert oct(hist.stat().st_mode & 0o777) == "0o700"
    assert oct((hist / "in").stat().st_mode & 0o777) == "0o700"

    p = PtyPane(tmp_path)  # an empty session doesn't wipe the history...
    assert p.close() == 0
    assert (hist / "items.jsonl").exists()
    p = PtyPane(tmp_path)  # ...but clearing on purpose does
    p.keys("C")
    time.sleep(0.3)
    assert p.close() == 0
    assert not hist.exists()


def test_peek_makes_small_copies_and_keeps_small_files(tmp_path, monkeypatch, capsys, mp4):
    from mediapane.peek import peek

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    big = tmp_path / "big.png"
    Image.new("RGB", (2000, 1000)).save(big)
    small = tmp_path / "small.png"
    Image.new("RGB", (300, 200)).save(small)
    out = peek(big)
    with Image.open(out) as im:
        assert im.size == (768, 384)
    assert peek(big) == out  # cached
    assert peek(small) == small  # already cheap: no copy
    assert cli.main(["peek", str(big), "--max", "256"]) == 0
    printed = capsys.readouterr().out.strip()
    assert printed.endswith(".png") and "\n" not in printed  # just the path
    with Image.open(peek(mp4)) as sheet:  # a video: 2x2 frames within the budget
        assert max(sheet.size) <= 768 and sheet.size[0] > sheet.size[1] * 1.2


def test_peek_errors_are_one_line(tmp_path, capsys):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"nope")
    assert cli.main(["peek", str(bad)]) == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "can't peek" in err


def test_pushers_cant_set_internal_fields(pane, png):
    header = {"added": "1e300", "digest": "x"}
    reply = ipc.request("push", png.read_bytes(), path=pane.sock, name="t.png", **header)
    assert reply["ok"]
    time.sleep(0.5)
    assert ipc.request("ping", path=pane.sock)["ok"]  # still alive and drawing
    assert time.strftime("%H:%M") in pane.text()  # shown with the arrival time


def test_bad_resume_times_fall_back_to_now():
    from mediapane.pane.items import _when

    now = time.time()
    for bad in ("1e300", "inf", "nan", -5, None, "x", now + 10 * 86400):
        assert abs(_when(bad) - now) < 5
    assert _when(now - 3600) == now - 3600


def test_mp_with_options_first_means_push(monkeypatch, png):
    got = {}
    monkeypatch.setattr(ipc, "push_file", lambda p, name=None, **m: got.update(m) or {"ok": True})
    assert cli.main(["--tag", "loss", "--step", "3", str(png)]) == 0
    assert got == {"tag": "loss", "step": "3"}


def test_folding_a_duplicate_keeps_following(pane, tmp_path):
    from test_pane import distinct_pngs

    a, b, c = distinct_pngs(tmp_path, 3)
    pane.push(a)
    pane.push(b)
    assert pane.push(a)["duplicate"]  # an old item again: shown, and following continues
    pane.push(c)
    pane.wait_for(lambda: "3 items · following" in pane.text(), "still following")


def test_identical_pushes_decoding_together_fold(pane, gif):
    import threading

    replies = []
    threads = [threading.Thread(target=lambda: replies.append(pane.push(gif))) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len({r["id"] for r in replies}) == 1
    assert ipc.request("ping", path=pane.sock)["items"] == 1


def test_resumed_items_still_fold_duplicates(tmp_path, png):
    p = PtyPane(tmp_path)
    p.push(png)
    assert p.close() == 0
    p = PtyPane(tmp_path, args=["--resume"])
    try:
        p.wait_for(lambda: ipc.request("ping", path=p.sock)["items"] == 1, "resumed")
        assert p.push(png)["duplicate"]
    finally:
        p.close()


def test_peek_handles_deep_float_and_transparent_images(tmp_path, monkeypatch):
    import numpy as np

    from mediapane.peek import peek

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    grad16 = tmp_path / "g16.png"
    Image.fromarray(np.tile(np.arange(0, 65536, 256, dtype=np.uint16), (64, 1))).save(grad16)
    flt = tmp_path / "f.tiff"
    Image.fromarray(np.linspace(-1, 1, 64 * 64, dtype=np.float32).reshape(64, 64)).save(flt)
    pal = tmp_path / "p.png"
    im = Image.new("P", (64, 64), 0)
    im.putpalette([0, 0, 0, 0, 0, 0])
    im.paste(1, (0, 0, 64, 4))  # black line; the rest is palette entry 0, transparent
    im.save(pal, transparency=0)
    for f in (grad16, flt):
        with Image.open(peek(f)) as out:
            lo, hi = out.convert("L").getextrema()
            assert lo < 10 and hi > 245  # the full range, not a blank image
    with Image.open(peek(pal)) as out:
        assert out.convert("L").getpixel((32, 40)) == 255  # transparent -> white, not black
        assert out.convert("L").getpixel((32, 1)) == 0


def test_peek_big_images_quietly(tmp_path, monkeypatch, capfd):

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)  # make 64x64 count as "huge"
    big = tmp_path / "big.png"
    Image.new("RGB", (2000, 50)).save(big)
    assert cli.main(["peek", str(big)]) == 0
    assert capfd.readouterr().err == ""
    assert Image.MAX_IMAGE_PIXELS == 1000  # restored afterwards
