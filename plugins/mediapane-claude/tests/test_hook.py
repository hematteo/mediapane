from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mediapane_claude import hook


def run_hook(monkeypatch, capsys, payload) -> tuple[int, str]:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    rc = hook.main()
    out, err = capsys.readouterr()
    assert out == ""  # stdout is Claude's: never write to it
    return rc, err


def event(tool: str, tool_input: dict, cwd: Path) -> dict:
    return {
        "session_id": "abc",
        "transcript_path": str(cwd / "t.jsonl"),
        "cwd": str(cwd),
        "hook_event_name": "PostToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
        "tool_response": {"success": True},
    }


def make(path: Path, age: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG fake")
    if age:
        t = time.time() - age
        os.utime(path, (t, t))
    return path


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "Read"])
def test_file_tools_push_media(tool, tmp_path, pane, monkeypatch, capsys):
    png = make(tmp_path / "fig.png", age=3600)  # Read shows old files too
    assert run_hook(monkeypatch, capsys, event(tool, {"file_path": str(png)}, tmp_path))[0] == 0
    assert len(pane.received) == 1
    header, data = pane.received[0]
    assert header["name"] == "fig.png" and header["origin"] == "claude"
    assert data == png.read_bytes()


def test_relative_path_resolves_against_cwd(tmp_path, pane, monkeypatch, capsys):
    make(tmp_path / "out" / "a.GIF")
    run_hook(monkeypatch, capsys, event("Write", {"file_path": "out/a.GIF"}, tmp_path))
    assert pane.names == ["a.GIF"]


def test_notebook_path(tmp_path, pane, monkeypatch, capsys):
    make(tmp_path / "x.png")
    run_hook(monkeypatch, capsys, event("NotebookEdit", {"notebook_path": "x.png"}, tmp_path))
    assert pane.names == ["x.png"]


@pytest.mark.parametrize(
    "tool_input",
    [{"file_path": "notes.md"}, {"file_path": "missing.png"}, {"file_path": 3}, {}],
)
def test_file_tools_ignore_non_media_and_missing(tool_input, tmp_path, pane, monkeypatch, capsys):
    (tmp_path / "notes.md").write_text("hi")
    run_hook(monkeypatch, capsys, event("Write", tool_input, tmp_path))
    assert pane.received == []


def test_other_tools_ignored(tmp_path, pane, monkeypatch, capsys):
    make(tmp_path / "x.png")
    run_hook(monkeypatch, capsys, event("Glob", {"file_path": "x.png"}, tmp_path))
    assert pane.received == []


def test_bash_pushes_recent_outputs_only(tmp_path, pane, monkeypatch, capsys):
    make(tmp_path / "new.png")
    make(tmp_path / "old.png", age=3600)
    make(tmp_path / "sub dir" / "spaced.mp4")
    make(tmp_path / "flag.webp")
    make(tmp_path / "redir.jpg")
    cmd = (
        "python plot.py old.png new.png 'sub dir/spaced.mp4' --out=flag.webp "
        "missing.png && echo hi>redir.jpg"
    )
    run_hook(monkeypatch, capsys, event("Bash", {"command": cmd}, tmp_path))
    assert pane.names == ["new.png", "spaced.mp4", "flag.webp", "redir.jpg"]
    assert all(h["origin"] == "claude" for h, _ in pane.received)


def test_bash_paths_inside_code_and_after_cd(tmp_path, pane, monkeypatch, capsys):
    make(tmp_path / "runs" / "loss.png")
    make(tmp_path / "abs.gif")
    cmd = f"cd runs && python -c \"plt.savefig('loss.png')\"; ls {tmp_path / 'abs.gif'}"
    run_hook(monkeypatch, capsys, event("Bash", {"command": cmd}, tmp_path))
    assert pane.names == ["loss.png", "abs.gif"]


def test_bash_unbalanced_quotes_fall_back_to_regex(tmp_path, pane, monkeypatch, capsys):
    make(tmp_path / "a.png")
    run_hook(monkeypatch, capsys, event("Bash", {"command": "echo 'oops a.png"}, tmp_path))
    assert pane.names == ["a.png"]


def test_bash_ignores_globs_and_dedupes(tmp_path, pane, monkeypatch, capsys):
    make(tmp_path / "a.png")
    cmd = "ls *.png a.png ./a.png a.png"
    run_hook(monkeypatch, capsys, event("Bash", {"command": cmd}, tmp_path))
    assert pane.names == ["a.png"]


def test_bash_caps_files(tmp_path, pane, monkeypatch, capsys):
    names = [f"f{i}.png" for i in range(15)]
    for n in names:
        make(tmp_path / n)
    run_hook(monkeypatch, capsys, event("Bash", {"command": "touch " + " ".join(names)}, tmp_path))
    assert pane.names == names[: hook.MAX_FILES]


def test_no_pane_is_fine(tmp_path, monkeypatch, capsys):
    png = make(tmp_path / "fig.png")
    payload = event("Write", {"file_path": str(png)}, tmp_path)
    assert run_hook(monkeypatch, capsys, payload) == (0, "")


@pytest.mark.parametrize(
    "garbage",
    ["", "not json", "[1, 2]", "null", '{"tool_name": "Bash", "tool_input": "x"}',
     '{"tool_name": "Bash", "tool_input": {"command": null}, "cwd": 5}',
     '{"tool_name": "Write", "tool_input": {"file_path": "\\u0000.png"}}'],
)  # fmt: skip
def test_garbage_never_fails(garbage, pane, monkeypatch, capsys):
    assert run_hook(monkeypatch, capsys, garbage) == (0, "")
    assert pane.received == []


def test_errors_are_swallowed(tmp_path, monkeypatch, capsys):
    png = make(tmp_path / "fig.png")

    def boom(*a, **k):
        raise RuntimeError("pane exploded")

    monkeypatch.setattr("mediapane.ipc.push_file", boom)
    payload = event("Write", {"file_path": str(png)}, tmp_path)
    assert run_hook(monkeypatch, capsys, payload) == (0, "")  # silent unless debugging
    monkeypatch.setenv("MEDIAPANE_DEBUG", "1")
    rc, err = run_hook(monkeypatch, capsys, payload)
    assert rc == 0 and "pane exploded" in err


def test_cli_hook_subprocess(tmp_path, pane):
    """The command Claude Code actually runs, through the core's plugin discovery."""
    png = make(tmp_path / "fig.png")
    for stdin in (json.dumps(event("Write", {"file_path": str(png)}, tmp_path)), "{{garbage"):
        r = subprocess.run(
            [sys.executable, "-m", "mediapane", "claude", "hook"],
            input=stdin,
            capture_output=True,
            text=True,
            cwd=tmp_path,
            timeout=30,
        )
        assert r.returncode == 0 and r.stdout == ""
    assert pane.names == ["fig.png"]


def test_installed_command_runs_the_hook(tmp_path, pane):
    from mediapane_claude import settings

    img = tmp_path / "out.png"
    img.write_bytes(b"\x89PNG fake")
    (tmp_path / "claude").write_text("a project file named like the command")
    event = {"tool_name": "Write", "cwd": str(tmp_path), "tool_input": {"file_path": "out.png"}}
    r = subprocess.run(
        settings.hook_command(), shell=True, cwd=tmp_path, input=json.dumps(event),
        capture_output=True, text=True, env=os.environ.copy(), timeout=30,
    )  # fmt: skip
    assert r.returncode == 0 and r.stdout == ""
    assert [h["name"] for h, _ in pane.received] == ["out.png"]


@pytest.mark.parametrize(
    "command",
    ["mp out.png", "cd x && mp --tag t out.png", "python make.py; mediapane push out.png",
     "/usr/local/bin/mp out.png", "mediapane peek out.png", "FOO=1 mp out.png",
     "uv run mp out.png", "python -m mediapane push out.png", "time mp out.png",
     "nohup mp out.png &", "env -i PATH=/bin mediapane peek out.png",
     "for f in *.png; do mp $f; done", "{ mp out.png; }", "cd out\nmp out.png",
     "python3 make.py && uvx mediapane peek out.png"],
)  # fmt: skip
def test_commands_that_push_themselves_are_left_alone(command, tmp_path, pane):
    (tmp_path / "out.png").write_bytes(b"\x89PNG")
    payload = {"tool_name": "Bash", "cwd": str(tmp_path), "tool_input": {"command": command}}
    assert hook.media_paths(payload) == []


def test_mediapanes_own_files_are_not_pushed(tmp_path, monkeypatch, pane):
    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    copy = cache / "mediapane" / "peek" / "fig-abc.png"
    copy.parent.mkdir(parents=True)
    copy.write_bytes(b"\x89PNG")
    payload = {"tool_name": "Read", "cwd": str(tmp_path), "tool_input": {"file_path": str(copy)}}
    assert hook.media_paths(payload) == []


@pytest.mark.parametrize(
    "command", ["python plot.py --out out.png", "cp a.png out.png", "echo mp > out.png"]
)
def test_other_commands_still_show_their_outputs(command, tmp_path, pane):
    (tmp_path / "out.png").write_bytes(b"\x89PNG")
    payload = {"tool_name": "Bash", "cwd": str(tmp_path), "tool_input": {"command": command}}
    assert hook.media_paths(payload) == [tmp_path / "out.png"]
