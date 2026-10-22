from __future__ import annotations

import io
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from mediapane import cli, plugins
from PIL import Image, ImageDraw


def make(path: Path, size=(40, 30), color=(200, 40, 40), dot: tuple[int, int] | None = None):
    im = Image.new("RGB", size, color)
    if dot:
        ImageDraw.Draw(im).rectangle([dot, (dot[0] + 3, dot[1] + 3)], fill=(0, 255, 0))
    im.save(path)
    return path


def diff(*args: str) -> int:
    return cli.main(["diff", *map(str, args)])


def test_plugin_registers_command():
    api = plugins.load()
    assert "diff" in api.loaded
    assert api.commands["diff"].plugin == "diff"


def test_identical(tmp_path, pane, capsys):
    a, b = make(tmp_path / "a.png"), make(tmp_path / "b.png")
    assert diff(a, b) == 0
    assert "identical" in capsys.readouterr().out
    assert len(pane.received) == 1
    header, data = pane.received[0]
    assert header["tag"] == "diff: a.png vs b.png" and header["step"] == "side by side"
    im = Image.open(io.BytesIO(data))
    assert im.width == 2 * 40 + 8 and im.height > 30  # gap and a label strip


def test_different_is_one_series(tmp_path, pane, capsys):
    a = make(tmp_path / "a.png")
    b = make(tmp_path / "b.png", dot=(10, 10))  # 4x4 of 40x30 = 1.33%
    assert diff(a, b, "--name", "fig.png") == 0
    out = capsys.readouterr().out
    assert "1.33% of pixels differ" in out and "max 215/255" in out
    headers = [h for h, _ in pane.received]
    assert {h["tag"] for h in headers} == {"diff: fig.png"}
    assert [h["step"] for h in headers] == [
        "side by side",
        "flicker",
        "difference (1.33% changed)",
    ]
    assert [h["name"] for h in headers] == ["fig-side.png", "fig-flicker.png", "fig-diff.png"]

    flick = Image.open(io.BytesIO(pane.received[1][1]))
    assert flick.format == "PNG" and getattr(flick, "is_animated", False)
    assert flick.n_frames == 2
    assert flick.info.get("duration") == 500

    heat = Image.open(io.BytesIO(pane.received[2][1])).convert("RGB")
    top = heat.height - 30  # label strip above the image
    changed = heat.getpixel((11, top + 11))
    unchanged = heat.getpixel((30, top + 25))
    assert sum(changed) > 300 and sum(unchanged) < 150  # bright on dark


def test_small_differences_are_visible(tmp_path, pane, capsys):
    a = make(tmp_path / "a.png", color=(100, 100, 100))
    im = Image.new("RGB", (40, 30), (100, 100, 100))
    im.putpixel((5, 5), (101, 100, 100))
    im.save(tmp_path / "b.png")
    assert diff(a, tmp_path / "b.png", "--mode", "heat") == 0
    assert "max 1/255" in capsys.readouterr().out
    ((header, data),) = pane.received
    assert header["step"].startswith("difference (")
    heat = Image.open(io.BytesIO(data)).convert("RGB")
    top = heat.height - 30
    assert sum(heat.getpixel((5, top + 5))) > 3 * sum(heat.getpixel((20, top + 20)))


def test_different_sizes(tmp_path, pane, capsys):
    a = make(tmp_path / "a.png", size=(40, 30))
    b = make(tmp_path / "b.png", size=(80, 60))
    assert diff(a, b) == 0
    out = capsys.readouterr().out
    assert "scaled to A's 40x30" in out and "identical" in out
    side = Image.open(io.BytesIO(pane.received[0][1]))
    assert side.width == 2 * 40 + 8


def test_modes(tmp_path, pane):
    a, b = make(tmp_path / "a.png"), make(tmp_path / "b.png", dot=(0, 0))
    assert diff(a, b, "--mode", "flicker") == 0
    assert [h["step"] for h, _ in pane.received] == ["flicker"]


def test_animation_uses_first_frame(tmp_path, pane, capsys):
    frames = [Image.new("RGB", (20, 20), (i * 60, 0, 0)) for i in range(3)]
    frames[0].save(tmp_path / "a.gif", save_all=True, append_images=frames[1:], duration=50)
    frames[0].save(tmp_path / "b.png")
    assert diff(tmp_path / "a.gif", tmp_path / "b.png") == 0
    assert "identical" in capsys.readouterr().out


def test_transparent_pixels_compare_equal(tmp_path, pane, capsys):
    Image.new("RGBA", (10, 10), (255, 0, 0, 0)).save(tmp_path / "a.png")
    Image.new("RGBA", (10, 10), (0, 0, 255, 0)).save(tmp_path / "b.png")
    assert diff(tmp_path / "a.png", tmp_path / "b.png") == 0
    assert "identical" in capsys.readouterr().out


@pytest.mark.parametrize("missing", ["/dev/null", "nope.png"])
def test_one_side_missing(tmp_path, pane, capsys, missing):
    b = make(tmp_path / "b.png")
    assert diff(missing if missing.startswith("/") else tmp_path / missing, b) == 0
    assert "only B exists" in capsys.readouterr().out
    ((header, data),) = pane.received
    assert header["step"] == "B only" and data == b.read_bytes()
    assert diff(b, "/dev/null", "--name", "fig.png") == 0
    assert pane.received[1][0]["step"] == "A only" and pane.received[1][0]["name"] == "fig.png"


def test_unreadable_input(tmp_path, pane, capsys):
    (tmp_path / "junk.png").write_bytes(b"not an image")
    b = make(tmp_path / "b.png")
    assert diff(tmp_path / "junk.png", b) == 2
    assert "can't read" in capsys.readouterr().err
    assert diff("/dev/null", tmp_path / "missing.png") == 2
    assert pane.received == []


def test_no_pane(tmp_path, capsys):
    a, b = make(tmp_path / "a.png"), make(tmp_path / "b.png", dot=(1, 1))
    assert diff(a, b) == 1
    assert "mediapane open" in capsys.readouterr().err


def test_rejected(tmp_path, pane, capsys):
    pane.ok = False
    a, b = make(tmp_path / "a.png"), make(tmp_path / "b.png", dot=(1, 1))
    assert diff(a, b) == 1
    assert "bad image" in capsys.readouterr().err


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_git_difftool_added_and_deleted(tmp_path, pane, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=repo,
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    git("commit", "-q", "--allow-empty", "-m", "empty")
    make(repo / "fig.png")
    git("add", "fig.png")
    git("commit", "-q", "-m", "add")
    make(repo / "fig.png", dot=(5, 5))
    git("commit", "-q", "-am", "change")
    git("rm", "-q", "fig.png")
    git("commit", "-q", "-m", "delete")

    tool = f'"{sys.executable}" -m mediapane diff "$LOCAL" "$REMOTE" --name "$MERGED"'
    for old, new in (("HEAD~3", "HEAD~2"), ("HEAD~2", "HEAD~1"), ("HEAD~1", "HEAD")):
        git(
            "-c", f"difftool.mp.cmd={tool}", "difftool", "-y", "-t", "mp", old, new, "--", "fig.png"
        )
    steps = [h["step"] for h, _ in pane.received]
    assert steps == ["B only", "side by side", "flicker", "difference (1.33% changed)", "A only"]
    assert {h["tag"] for h, _ in pane.received} == {"diff: fig.png"}
