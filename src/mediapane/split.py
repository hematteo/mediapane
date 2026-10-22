"""`mediapane open`: start a pane in a new split next to the current one."""

from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess
import sys
import urllib.parse

# Variables the pane needs that a fresh split may not inherit (Ghostty's scripted splits start
# with its bare launch environment)
PASS_ENV = ("PATH", "MEDIAPANE_SOCKET", "MEDIAPANE_TRANSFER", "XDG_CACHE_HOME")


def pane_command() -> list[str]:
    from .ipc import default_socket

    # The socket explicitly: a new split may not inherit MEDIAPANE_SOCKET / XDG_CACHE_HOME
    return [sys.executable, "-m", "mediapane", "pane", "--socket", str(default_socket())]


def open_split(down: bool = False) -> str:
    """Open a pane beside this one; returns which terminal or multiplexer did it."""
    cmd = pane_command()
    if os.environ.get("TMUX"):
        subprocess.run(
            ["tmux", "split-window", "-v" if down else "-h", "-d", "-l", "45%", shlex.join(cmd)],
            check=True,
        )
        return "tmux"
    if os.environ.get("KITTY_WINDOW_ID") and shutil.which("kitten"):
        r = subprocess.run(
            [
                "kitten",
                "@",
                "launch",
                "--location",
                "hsplit" if down else "vsplit",
                "--keep-focus",
                "--cwd",
                "current",
                "--copy-env",
                *cmd,
            ],
            capture_output=True,
            text=True,
        )
        if r.returncode:
            raise RuntimeError(
                "kitty refused (enable allow_remote_control in kitty.conf): " + r.stderr.strip()
            )
        return "kitty"
    if os.environ.get("WEZTERM_PANE") and shutil.which("wezterm"):
        subprocess.run(
            [
                "wezterm",
                "cli",
                "split-pane",
                "--bottom" if down else "--right",
                "--percent",
                "45",
                "--",
                *cmd,
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["wezterm", "cli", "activate-pane", "--pane-id", os.environ["WEZTERM_PANE"]],
            check=False,
            capture_output=True,
        )
        return "WezTerm"
    if os.environ.get("TERM_PROGRAM") == "ghostty" and sys.platform == "darwin":
        _ghostty(cmd, down)
        return "Ghostty"
    raise RuntimeError(
        "don't know how to split this terminal; run `mediapane` in a new pane "
        "yourself (tmux, Ghostty on macOS, kitty and WezTerm are automatic)"
    )


def own_tty() -> str | None:
    """The terminal this process belongs to, even when its own stdio isn't a tty (e.g. when an
    agent runs it): the first ancestor with a controlling terminal."""
    for fd in (0, 1, 2):
        try:
            return os.ttyname(fd)
        except OSError:
            pass
    pid = os.getpid()
    while pid > 1:
        out = subprocess.run(
            ["ps", "-o", "tty=,ppid=", "-p", str(pid)], capture_output=True, text=True
        ).stdout.split()
        if len(out) < 2:
            break
        if out[0] not in ("?", "??"):
            return f"/dev/{out[0]}"
        pid = int(out[1])
    return None


_SPLIT = """
on run argv
  set cmd to item 1 of argv
  set dir to item 2 of argv
  set marker to item 3 of argv
  set envList to items 4 thru -1 of argv
  tell application "Ghostty"
    set main to missing value
    if marker is not "" then
      repeat 20 times
        set found to every terminal whose working directory is marker
        if found is not {} then
          set main to item 1 of found
          exit repeat
        end if
        delay 0.05
      end repeat
    end if
    if main is missing value then set main to focused terminal of selected tab of front window
    set cfg to new surface configuration
    set command of cfg to cmd
    set environment variables of cfg to envList
    if dir is "down" then
      split main direction down with configuration cfg
    else
      split main direction right with configuration cfg
    end if
    focus main
    return id of main
  end tell
end run
"""

# `tab` inside a Ghostty tell block is Ghostty's tab class, hence character id 9
_DIRS = """
set sep to character id 9
tell application "Ghostty"
  set out to ""
  repeat with t in terminals
    set out to out & (id of t) & sep & (working directory of t) & linefeed
  end repeat
  return out
end tell
"""


def _ghostty(cmd: list[str], down: bool) -> None:
    """Split via Ghostty's AppleScript (macOS, Ghostty 1.3+). Its scripting can't map a tty to a
    terminal, and titles can be pinned or animated (Claude Code's spinner), so mark our pane with
    a fake working directory (OSC 7), find the terminal reporting it, then put the real one back.
    """
    # Ghostty starts a split's command with `exec -l`, which prefixes argv[0] with "-"; a venv's
    # python finds its prefix from argv[0] and then can't load its stdlib. env doesn't mind.
    command = shlex.join(["/usr/bin/env", *cmd])
    env = [f"{k}={os.environ[k]}" for k in PASS_ENV if k in os.environ]
    env.append("MEDIAPANE_CLOSE_GHOSTTY=1")
    tty = own_tty()
    marker = f"/tmp/mediapane-open-{os.getpid()}" if tty else ""
    before = _ghostty_dirs() if tty else {}
    main_id = ""
    try:
        if tty:
            _osc7(tty, marker)
        r = subprocess.run(
            ["osascript", "-", command, "down" if down else "right", marker, *env],
            input=_SPLIT,
            text=True,
            check=True,
            capture_output=True,
        )
        main_id = r.stdout.strip()
    finally:
        if tty:
            _osc7(tty, before.get(main_id) or os.getcwd())


def _ghostty_dirs() -> dict[str, str]:
    """Each Ghostty terminal's id and working directory."""
    out = subprocess.run(["osascript", "-"], input=_DIRS, text=True, capture_output=True).stdout
    return dict(line.split("\t", 1) for line in out.splitlines() if "\t" in line)


def _osc7(tty: str, path: str) -> None:
    """Report `path` as the working directory of the terminal on `tty`."""
    with open(tty, "w") as f:
        f.write(f"\x1b]7;file://{socket.gethostname()}{urllib.parse.quote(path)}\x07")
